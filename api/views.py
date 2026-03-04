from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify
from jwt import PyJWKClient
import jwt
import logging
import json
import re
import math
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from django.db import transaction
from rest_framework import generics
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.views import APIView
from .models import ChatMessage, ChatThread, Website
from .serializers import (
    ChatMessageSerializer,
    ChatThreadListItemSerializer,
    ChatThreadSerializer,
    EmailOrUsernameTokenObtainPairSerializer,
    UserProfileSerializer,
    WebsiteSerializer,
)

User = get_user_model()

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
logger = logging.getLogger(__name__)
TOKENS_PER_CREDIT = 10_000


def _username_from_email(email: str) -> str:
    base = (email.split("@")[0] or "user").lower().replace(" ", "")
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}{suffix}"
        suffix += 1
    return username


def _is_identity_question(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False

    identity_patterns = [
        "who are you",
        "what are you",
        "whats your name",
        "what is your name",
        "your name",
        "who r you",
        "are you ai",
        "what do you do",
    ]
    return any(pattern in normalized for pattern in identity_patterns)


SGEN_WEB_IDENTITY_REPLY = (
    "I am Sgen Web. I am here to help you create websites with AI, including planning pages, "
    "generating sections, improving copy, and guiding you from idea to launch."
)

SGEN_WEB_MASTER_PROMPT = """
You are Sgen Web, a principal frontend architect and senior UI/UX engineer.
You specialize in production-grade responsive websites.

For website/code generation requests, follow these strict rules:

1) Technology constraints:
- Use only HTML, CSS, and JavaScript.
- No frameworks, no libraries, no external CSS/JS.
- One complete HTML document output.
- Exactly one <style> tag and one <script> tag.
- Use only system fonts.
- Keep output iframe srcdoc-compatible.

2) Quality bar:
- Premium modern UI with clear visual hierarchy.
- Business-ready copy (no lorem ipsum).
- Smooth hover/active states and transitions.
- Clean, readable, production-ready code.
- Keep output concise enough to avoid truncation: avoid unnecessary blank lines/repetition and keep CSS/JS compact.

3) Responsive requirements:
- Mobile-first approach.
- Must work on mobile (<768px), tablet (768px-1024px), desktop (>1024px).
- Use Grid/Flexbox, relative units, and media queries.
- No horizontal scroll on mobile.
- Touch-friendly controls.
- Navbar must adapt on small screens.

4) SPA behavior:
- Implement pages/sections for Home, About, Services (or Features), Contact.
- Navigation must be JavaScript-driven without page reload.
- Active navigation state must update correctly.
- If using hidden pages, ensure one page is visible on initial load and active state reveals pages.

5) Images:
- Use high-quality images from https://images.unsplash.com/
- Every image URL must include: ?auto=format&fit=crop&w=1200&q=80
- Images must be responsive and never overflow.

6) Forms and interactions:
- Contact form must include JavaScript validation.
- No dead buttons or broken interactions.
- If user asks for a functional tool/app (e.g., calculator, converter, todo, quiz, dashboard widgets),
  implement fully working JavaScript logic for all primary actions.
- Example for calculator: working number input, + - * /, decimal handling, clear/reset, delete/backspace,
  equals evaluation, and safe invalid-operation handling (no crashes/NaN leaks in UI).
- Visual quality must be polished: modern spacing, strong typography hierarchy, clear states, and responsive layout.

7) Output format:
- Return RAW JSON only in this structure:
{
  "message": "Short professional confirmation sentence",
  "code": "<FULL VALID HTML DOCUMENT>"
}
- No markdown, no explanation, no extra text.
- For updates, always return the complete updated HTML document in "code" (never partial snippets/diffs).
- Do not return Python, Java, C++, terminal scripts, pseudocode, or any non-web language output.
- Even for tools like calculators/converters/todos, always return browser-runnable HTML with embedded CSS and JavaScript.

Before final output, self-check responsiveness, navigation behavior, media-query usage, visibility on initial load, and mobile overflow.
""".strip()


def _is_website_or_code_request(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False

    keywords = [
        "website",
        "web site",
        "landing page",
        "web app",
        "application",
        "app",
        "tool",
        "calculator",
        "converter",
        "todo",
        "dashboard",
        "ui",
        "ux",
        "frontend",
        "backend",
        "full stack",
        "code",
        "coding",
        "html",
        "css",
        "javascript",
        "typescript",
        "react",
        "next js",
        "nextjs",
        "api",
    ]
    return any(keyword in normalized for keyword in keywords)


def _extract_generated_payload(raw_text: str) -> tuple[str, str]:
    """
    Try to split assistant output into user-facing message and generated code.
    Falls back gracefully for plain-text model responses.
    """
    text = (raw_text or "").strip()
    if not text:
        return "", ""

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            message = str(payload.get("message") or "").strip()
            code = str(payload.get("code") or "").strip()
            if message or code:
                return message, code
    except Exception:
        pass

    fenced_json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced_json_match:
        try:
            payload = json.loads(fenced_json_match.group(1))
            if isinstance(payload, dict):
                message = str(payload.get("message") or "").strip()
                code = str(payload.get("code") or "").strip()
                if message or code:
                    return message, code
        except Exception:
            pass

    object_match = re.search(r"\{[\s\S]*\"code\"[\s\S]*\}", text)
    if object_match:
        try:
            payload = json.loads(object_match.group(0))
            if isinstance(payload, dict):
                message = str(payload.get("message") or "").strip()
                code = str(payload.get("code") or "").strip()
                if message or code:
                    return message, code
        except Exception:
            pass

    fenced_match = re.search(r"```(?:html)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced_match:
        extracted_code = fenced_match.group(1).strip()
        plain_message = re.sub(r"```(?:html)?\s*.*?```", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        return plain_message, extracted_code

    if "<html" in text.lower() and "</html>" in text.lower():
        return "", text

    return text, ""


def _extract_title_from_html(code: str) -> str:
    if not code:
        return ""
    title_match = re.search(r"<title>\s*(.*?)\s*</title>", code, flags=re.IGNORECASE | re.DOTALL)
    if not title_match:
        return ""
    return re.sub(r"\s+", " ", title_match.group(1)).strip()


def _is_complete_html_document(code: str) -> bool:
    normalized = (code or "").strip().lower()
    if not normalized:
        return False
    return (
        "<html" in normalized
        and "</html>" in normalized
        and "<style" in normalized
        and "<script" in normalized
        and "<body" in normalized
    )


def _is_calculator_request(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return "calculator" in normalized or "calculate" in normalized


def _looks_like_non_web_code(text: str) -> bool:
    lowered = (text or "").lower()
    markers = [
        "```python",
        "def ",
        "print(",
        "input(",
        "public static void main",
        "#include <",
        "using namespace std",
    ]
    return any(marker in lowered for marker in markers)


def _default_calculator_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Smart Calculator</title>
  <style>
    :root {
      --bg1: #0f172a;
      --bg2: #1e293b;
      --card: #0b1220;
      --muted: #94a3b8;
      --text: #f8fafc;
      --accent: #f97316;
      --accent-2: #fb923c;
      --btn: #1f2937;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: linear-gradient(145deg, var(--bg1), var(--bg2));
      display: grid;
      place-items: center;
      padding: 20px;
    }
    .app {
      width: min(100%, 390px);
      background: rgba(11, 18, 32, 0.9);
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 22px;
      padding: 16px;
      box-shadow: 0 20px 48px rgba(2, 6, 23, 0.55);
      backdrop-filter: blur(8px);
    }
    .title {
      margin: 0 0 10px;
      font-size: 0.9rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .display {
      width: 100%;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: #020617;
      color: var(--text);
      border-radius: 14px;
      padding: 14px;
      min-height: 86px;
      text-align: right;
      margin-bottom: 14px;
      overflow: hidden;
    }
    .expression {
      min-height: 20px;
      font-size: 0.9rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .result {
      margin-top: 6px;
      font-size: clamp(1.6rem, 5vw, 2.2rem);
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .keys {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    button {
      border: 0;
      border-radius: 12px;
      background: var(--btn);
      color: var(--text);
      font-size: 1rem;
      font-weight: 600;
      min-height: 54px;
      cursor: pointer;
      transition: transform 0.08s ease, filter 0.15s ease;
    }
    button:hover { filter: brightness(1.1); }
    button:active { transform: translateY(1px) scale(0.99); }
    .op { background: #334155; }
    .accent {
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #111827;
    }
    .span-2 { grid-column: span 2; }
    .hint {
      margin-top: 12px;
      font-size: 0.78rem;
      color: var(--muted);
      text-align: center;
    }
  </style>
</head>
<body>
  <main class="app">
    <h1 class="title">Smart Calculator</h1>
    <section class="display" aria-live="polite">
      <div class="expression" id="expression"></div>
      <div class="result" id="result">0</div>
    </section>
    <section class="keys">
      <button data-action="clear" class="op">C</button>
      <button data-action="backspace" class="op">⌫</button>
      <button data-value="/" class="op">÷</button>
      <button data-value="*" class="op">×</button>
      <button data-value="7">7</button>
      <button data-value="8">8</button>
      <button data-value="9">9</button>
      <button data-value="-" class="op">−</button>
      <button data-value="4">4</button>
      <button data-value="5">5</button>
      <button data-value="6">6</button>
      <button data-value="+" class="op">+</button>
      <button data-value="1">1</button>
      <button data-value="2">2</button>
      <button data-value="3">3</button>
      <button data-action="equals" class="accent">=</button>
      <button data-value="0" class="span-2">0</button>
      <button data-value=".">.</button>
      <button data-action="negate" class="op">±</button>
    </section>
    <p class="hint">Keyboard supported: 0-9, + - * /, Enter, Backspace, Esc</p>
  </main>
  <script>
    const expressionEl = document.getElementById("expression");
    const resultEl = document.getElementById("result");
    const keys = document.querySelector(".keys");

    let expression = "";
    let current = "0";
    let justEvaluated = false;

    function render() {
      expressionEl.textContent = expression;
      resultEl.textContent = current || "0";
    }

    function safeEval(exp) {
      try {
        if (!/^[0-9+\\-*/.()\\s]+$/.test(exp)) return null;
        const value = Function('"use strict"; return (' + exp + ')')();
        if (typeof value !== "number" || !Number.isFinite(value)) return null;
        return Number(value.toFixed(10)).toString();
      } catch {
        return null;
      }
    }

    function appendNumber(ch) {
      if (justEvaluated) {
        expression = "";
        current = "0";
        justEvaluated = false;
      }
      if (ch === "." && current.includes(".")) return;
      current = current === "0" && ch !== "." ? ch : current + ch;
      render();
    }

    function appendOperator(op) {
      if (justEvaluated) justEvaluated = false;
      if (current !== "") {
        expression += current;
      }
      if (!expression) return;
      expression = expression.replace(/[+\\-*/]\\s*$/, "") + op;
      current = "";
      render();
    }

    function equals() {
      const exp = expression + (current || "");
      if (!exp) return;
      const out = safeEval(exp);
      expression = exp;
      current = out ?? "Error";
      justEvaluated = true;
      render();
    }

    function clearAll() {
      expression = "";
      current = "0";
      justEvaluated = false;
      render();
    }

    function backspace() {
      if (justEvaluated) {
        clearAll();
        return;
      }
      if (current && current !== "0") {
        current = current.slice(0, -1) || "0";
      } else {
        expression = expression.slice(0, -1);
      }
      render();
    }

    function negate() {
      if (!current || current === "0") return;
      current = current.startsWith("-") ? current.slice(1) : "-" + current;
      render();
    }

    function handleValue(v) {
      if (/^[0-9.]$/.test(v)) appendNumber(v);
      else if (/^[+\\-*/]$/.test(v)) appendOperator(v);
    }

    keys.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const value = btn.dataset.value;
      const action = btn.dataset.action;
      if (value) handleValue(value);
      if (action === "equals") equals();
      if (action === "clear") clearAll();
      if (action === "backspace") backspace();
      if (action === "negate") negate();
    });

    document.addEventListener("keydown", (e) => {
      if (/[0-9.+\\-*/]/.test(e.key)) handleValue(e.key);
      if (e.key === "Enter" || e.key === "=") equals();
      if (e.key === "Backspace") backspace();
      if (e.key === "Escape") clearAll();
    });

    render();
  </script>
</body>
</html>"""


def _build_unique_domain(owner, name: str) -> str:
    base = slugify(name or "untitled")[:40] or "untitled"
    owner_slug = slugify(owner.username)[:20] or f"user-{owner.id}"
    candidate = f"{base}-{owner_slug}-{owner.id}"
    counter = 1
    while Website.objects.filter(domain=candidate).exists():
        counter += 1
        candidate = f"{base}-{owner_slug}-{owner.id}-{counter}"
    return candidate


def _openrouter_error_detail(exc: HTTPError) -> str:
    try:
        raw_error = exc.read().decode("utf-8")
        error_data = json.loads(raw_error)
        return (
            error_data.get("error", {}).get("message")
            or error_data.get("detail")
            or "OpenRouter request failed."
        )
    except Exception:
        return "OpenRouter request failed."


def _is_token_credit_error(detail: str) -> bool:
    text = (detail or "").lower()
    patterns = [
        "requires more credits",
        "fewer max_tokens",
        "can only afford",
        "requested up to",
    ]
    return any(p in text for p in patterns)


def _estimate_tokens_for_messages(messages: list[dict]) -> int:
    total_chars = 0
    for message in messages or []:
        content = str((message or {}).get("content") or "")
        total_chars += len(content)
    # Rough estimate: ~4 chars per token for English-heavy content.
    return max(1, math.ceil(total_chars / 4))


def _credits_for_tokens(total_tokens: int) -> int:
    tokens = max(0, int(total_tokens or 0))
    if tokens == 0:
        return 0
    return math.ceil(tokens / TOKENS_PER_CREDIT)


def _extract_usage_tokens(payload: dict) -> tuple[int, int, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _get_latest_generated_state(thread: ChatThread) -> tuple[str, int | None]:
    """
    Returns the most recent generated code and website id found in assistant metadata.
    """
    latest_messages = thread.messages.order_by("-sequence", "-created_at")
    for msg in latest_messages:
        metadata = msg.metadata or {}
        website_id = metadata.get("website_id")
        if not website_id:
            website_payload = metadata.get("website") or {}
            if isinstance(website_payload, dict):
                website_id = website_payload.get("id")

        if website_id:
            website = Website.objects.filter(id=website_id, owner=thread.owner).only("id", "code").first()
            if website and website.code.strip():
                return website.code.strip(), website.id

        # Legacy fallback for older metadata that may still contain code.
        legacy_code = metadata.get("code")
        if isinstance(legacy_code, str) and legacy_code.strip():
            return legacy_code.strip(), website_id
    return "", None


class LoginView(TokenObtainPairView):
    serializer_class = EmailOrUsernameTokenObtainPairSerializer


class RegisterView(generics.GenericAPIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""
        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()

        if not email:
            return Response({"detail": "email is required."}, status=status.HTTP_400_BAD_REQUEST)
        if "@" not in email:
            return Response({"detail": "valid email is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not password:
            return Response({"detail": "password is required."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "User with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(
            username=_username_from_email(email),
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": UserProfileSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class GoogleLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        id_token = request.data.get("id_token")
        if not id_token:
            return Response(
                {"detail": "id_token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        audience = settings.GOOGLE_OAUTH_CLIENT_IDS
        if not audience:
            return Response(
                {"detail": "Google OAuth client IDs are not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            jwks_client = PyJWKClient(GOOGLE_JWKS_URL)
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            payload = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=audience if len(audience) > 1 else audience[0],
                issuer=GOOGLE_ISSUERS,
                leeway=60,
            )
        except Exception as exc:
            logger.exception("Google token validation failed")
            detail = "Invalid Google token."
            if settings.DEBUG:
                detail = f"Invalid Google token: {exc}"
            return Response(
                {"detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = payload.get("email")
        if not email:
            return Response(
                {"detail": "Google account email is missing."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if payload.get("email_verified") is not True:
            return Response(
                {"detail": "Google email is not verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sub = payload.get("sub")
        first_name = payload.get("given_name", "")
        last_name = payload.get("family_name", "")
        picture = payload.get("picture", "")

        user = User.objects.filter(email=email).first()
        if user:
            if user.google_sub and user.google_sub != sub:
                return Response(
                    {"detail": "This email is already linked to another Google account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            user = User.objects.create_user(
                username=_username_from_email(email),
                email=email,
                password=None,
            )

        user.google_sub = sub
        user.first_name = first_name
        user.last_name = last_name
        user.avatar_url = picture
        user.save(update_fields=["google_sub", "first_name", "last_name", "avatar_url"])

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "avatar_url": user.avatar_url,
                },
            },
            status=status.HTTP_200_OK,
        )


class MeView(generics.RetrieveAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class LogoutView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"detail": "refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except AttributeError:
            # Blacklist app may not be enabled; frontend should still clear local tokens.
            return Response(
                {"detail": "Logged out on client. Token blacklist is not enabled."},
                status=status.HTTP_200_OK,
            )
        except Exception:
            return Response(
                {"detail": "Invalid refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"detail": "Logged out successfully."}, status=status.HTTP_200_OK)
class WebsiteListCreateView(generics.ListCreateAPIView):
    serializer_class = WebsiteSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.request.user.websites.all()

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class WebsiteDeployView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        website = Website.objects.filter(id=pk, owner=request.user).first()
        if not website:
            return Response({"detail": "website not found."}, status=status.HTTP_404_NOT_FOUND)

        if not website.code.strip():
            return Response(
                {"detail": "No generated code found for this website."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not website.deploy_url:
            website.deploy_url = f"https://{website.domain}.sgenweb.app"
            website.deployed_at = timezone.now()
            website.save(update_fields=["deploy_url", "deployed_at", "updated_at"])

        return Response(WebsiteSerializer(website).data, status=status.HTTP_200_OK)


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        websites = request.user.websites.order_by("-updated_at")
        return Response(
            {
                "credits": request.user.credits,
                "website_count": websites.count(),
                "websites": WebsiteSerializer(websites, many=True).data,
            },
            status=status.HTTP_200_OK,
        )


class ChatThreadListView(generics.ListAPIView):
    serializer_class = ChatThreadListItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.request.user.chat_threads.filter(is_archived=False).order_by("-updated_at")


class ChatThreadDetailView(generics.RetrieveAPIView):
    serializer_class = ChatThreadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.request.user.chat_threads.all()


class ChatCompleteView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.OPENROUTER_API_KEY:
            return Response(
                {"detail": "OPENROUTER_API_KEY is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        user_prompt = (request.data.get("message") or "").strip()
        is_code_request = _is_website_or_code_request(user_prompt)
        if not user_prompt:
            return Response(
                {"detail": "message is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        thread_id = request.data.get("thread_id")
        title = (request.data.get("title") or "").strip()

        if thread_id:
            thread = ChatThread.objects.filter(id=thread_id, owner=request.user).first()
            if not thread:
                return Response(
                    {"detail": "thread not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            thread = ChatThread.objects.create(
                owner=request.user,
                title=title or user_prompt[:60] or "New chat",
            )

        previous_code = ""
        previous_website_id = None
        if is_code_request:
            previous_code, previous_website_id = _get_latest_generated_state(thread)

        user_message = ChatMessage.objects.create(
            thread=thread,
            role=ChatMessage.Role.USER,
            content=user_prompt,
        )

        if _is_identity_question(user_prompt):
            assistant_message = ChatMessage.objects.create(
                thread=thread,
                role=ChatMessage.Role.ASSISTANT,
                content=SGEN_WEB_IDENTITY_REPLY,
            )
            thread.save(update_fields=["updated_at"])
            return Response(
                {
                    "thread": ChatThreadSerializer(thread).data,
                    "user_message": ChatMessageSerializer(user_message).data,
                    "assistant_message": ChatMessageSerializer(assistant_message).data,
                },
                status=status.HTTP_200_OK,
            )

        history = thread.messages.order_by("sequence", "created_at").values("role", "content")
        model_messages = [{"role": m["role"], "content": m["content"]} for m in history]

        if is_code_request:
            model_messages = [{"role": "system", "content": SGEN_WEB_MASTER_PROMPT}, *model_messages]
            if previous_code:
                model_messages = [
                    {
                        "role": "system",
                        "content": (
                            "Current website code state (apply new user edits on top of this):\n\n"
                            f"{previous_code}\n\n"
                            'Return full updated code in JSON key "code".'
                        ),
                    },
                    *model_messages,
                ]

        estimated_prompt_tokens = _estimate_tokens_for_messages(model_messages)
        estimated_total_tokens = estimated_prompt_tokens + settings.OPENROUTER_MAX_TOKENS
        estimated_credits_required = _credits_for_tokens(estimated_total_tokens)
        if request.user.credits < estimated_credits_required:
            return Response(
                {
                    "detail": (
                        f"Insufficient credits for this request. Estimated required credits: "
                        f"{estimated_credits_required}, available: {request.user.credits}. "
                        "Please buy credits and try again."
                    ),
                    "estimated_required_credits": estimated_credits_required,
                    "credits_remaining": request.user.credits,
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        data = None
        token_attempts = [settings.OPENROUTER_MAX_TOKENS]
        if settings.OPENROUTER_MAX_TOKENS > 1024:
            token_attempts.append(1024)
        if settings.OPENROUTER_MAX_TOKENS > 768:
            token_attempts.append(768)

        try:
            for idx, max_tokens in enumerate(token_attempts):
                try:
                    payload = json.dumps(
                        {
                            "model": settings.OPENROUTER_MODEL,
                            "messages": model_messages,
                            "max_tokens": max_tokens,
                        }
                    ).encode("utf-8")
                    req = urllib_request.Request(
                        settings.OPENROUTER_URL,
                        data=payload,
                        headers={
                            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urllib_request.urlopen(req, timeout=45) as resp:
                        raw_body = resp.read().decode("utf-8")
                        data = json.loads(raw_body)
                    break
                except HTTPError as exc:
                    detail = _openrouter_error_detail(exc)
                    should_retry = _is_token_credit_error(detail) and idx < len(token_attempts) - 1
                    if should_retry:
                        continue
                    return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)
        except URLError:
            return Response(
                {"detail": "Could not reach OpenRouter."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            logger.exception("Unexpected OpenRouter error")
            return Response(
                {"detail": "Unexpected error from OpenRouter."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            if not isinstance(data, dict):
                return Response(
                    {"detail": "Unexpected OpenRouter response format."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            if data.get("error"):
                detail = data.get("error", {}).get("message") or data.get("detail") or "OpenRouter request failed."
                return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)

            first_choice = data.get("choices", [{}])[0]
            assistant_text = (first_choice.get("message", {}).get("content", "")).strip()
            finish_reason = str(first_choice.get("finish_reason") or "").strip().lower()
            if not assistant_text:
                return Response(
                    {"detail": "Model returned empty response."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
        except Exception:
            logger.exception("Unexpected OpenRouter payload parsing error")
            return Response(
                {"detail": "Unexpected error from OpenRouter."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        assistant_content = assistant_text
        assistant_metadata = {}
        current_code = ""
        current_website_data = None
        prompt_tokens, completion_tokens, total_tokens = _extract_usage_tokens(data)
        if total_tokens <= 0:
            total_tokens = estimated_prompt_tokens + _estimate_tokens_for_messages(
                [{"content": assistant_text, "role": "assistant"}]
            )
        credits_to_charge = _credits_for_tokens(total_tokens)
        credits_remaining = request.user.credits

        if is_code_request:
            parsed_message, parsed_code = _extract_generated_payload(assistant_text)
            if parsed_message:
                assistant_content = parsed_message

            if not parsed_code and _is_calculator_request(user_prompt) and _looks_like_non_web_code(assistant_text):
                parsed_code = _default_calculator_html()
                assistant_content = "Generated a browser-based calculator with full functionality."

            if parsed_code:
                if not _is_complete_html_document(parsed_code):
                    if _is_calculator_request(user_prompt):
                        parsed_code = _default_calculator_html()
                        if not parsed_message:
                            assistant_content = "Calculator application created with full functionality."
                    else:
                        parsed_code = ""
                        if finish_reason == "length":
                            assistant_content = (
                                "Generation was truncated by token limits before full HTML completed. "
                                "Increase OPENROUTER_MAX_TOKENS or use a model with higher output limits, then retry."
                            )
                        else:
                            assistant_content = (
                                "I could not generate valid full HTML for preview this time. "
                                "Please try again with a clearer request."
                            )

            if parsed_code:
                if not parsed_message:
                    assistant_content = "Website generated successfully."
                current_code = parsed_code

                website_name = _extract_title_from_html(parsed_code) or thread.title or "Untitled Website"
                website = None
                if previous_website_id:
                    website = Website.objects.filter(id=previous_website_id, owner=request.user).first()

                if website:
                    website.name = website_name[:255]
                    website.code = parsed_code
                    website.save(update_fields=["name", "code", "updated_at"])
                else:
                    website = Website.objects.create(
                        owner=request.user,
                        name=website_name[:255],
                        code=parsed_code,
                        domain=_build_unique_domain(request.user, website_name),
                    )
                assistant_metadata["website_id"] = website.id
                current_website_data = WebsiteSerializer(website).data

            if parsed_message and not parsed_code:
                assistant_content = parsed_message

        if credits_to_charge > 0:
            with transaction.atomic():
                fresh_user = User.objects.select_for_update().get(id=request.user.id)
                if fresh_user.credits < credits_to_charge:
                    return Response(
                        {
                            "detail": (
                                f"Insufficient credits to process this request. Needed: {credits_to_charge}, "
                                f"available: {fresh_user.credits}. Please buy credits and try again."
                            ),
                            "required_credits": credits_to_charge,
                            "credits_remaining": fresh_user.credits,
                        },
                        status=status.HTTP_402_PAYMENT_REQUIRED,
                    )
                fresh_user.credits -= credits_to_charge
                fresh_user.save(update_fields=["credits"])
                credits_remaining = fresh_user.credits
                request.user.credits = fresh_user.credits

        assistant_message = ChatMessage.objects.create(
            thread=thread,
            role=ChatMessage.Role.ASSISTANT,
            content=assistant_content,
            metadata=assistant_metadata,
        )

        thread.save(update_fields=["updated_at"])
        return Response(
            {
                "thread": ChatThreadSerializer(thread).data,
                "user_message": ChatMessageSerializer(user_message).data,
                "assistant_message": ChatMessageSerializer(assistant_message).data,
                "previous_code": previous_code if is_code_request else "",
                "current_code": current_code if is_code_request else "",
                "current_website": current_website_data if is_code_request else None,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "credits_charged": credits_to_charge,
                },
                "credits_remaining": credits_remaining,
            },
            status=status.HTTP_200_OK,
        )
