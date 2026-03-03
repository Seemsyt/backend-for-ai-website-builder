from django.conf import settings
from django.contrib.auth import get_user_model
from jwt import PyJWKClient
import jwt
import logging
import json
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from rest_framework import generics
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.views import APIView
from .models import ChatMessage, ChatThread
from .serializers import (
    ChatMessageSerializer,
    ChatThreadSerializer,
    EmailOrUsernameTokenObtainPairSerializer,
    UserProfileSerializer,
    WebsiteSerializer,
)

User = get_user_model()

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
logger = logging.getLogger(__name__)


def _username_from_email(email: str) -> str:
    base = (email.split("@")[0] or "user").lower().replace(" ", "")
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}{suffix}"
        suffix += 1
    return username


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


class ChatCompleteView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.OPENROUTER_API_KEY:
            return Response(
                {"detail": "OPENROUTER_API_KEY is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        user_prompt = (request.data.get("message") or "").strip()
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

        user_message = ChatMessage.objects.create(
            thread=thread,
            role=ChatMessage.Role.USER,
            content=user_prompt,
        )

        history = thread.messages.order_by("sequence", "created_at").values("role", "content")
        model_messages = [{"role": m["role"], "content": m["content"]} for m in history]

        try:
            payload = json.dumps(
                {
                    "model": settings.OPENROUTER_MODEL,
                    "messages": model_messages,
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

        except HTTPError as exc:
            try:
                raw_error = exc.read().decode("utf-8")
                error_data = json.loads(raw_error)
                detail = (
                    error_data.get("error", {}).get("message")
                    or error_data.get("detail")
                    or "OpenRouter request failed."
                )
            except Exception:
                detail = "OpenRouter request failed."
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

            assistant_text = (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            ).strip()
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

        assistant_message = ChatMessage.objects.create(
            thread=thread,
            role=ChatMessage.Role.ASSISTANT,
            content=assistant_text,
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
