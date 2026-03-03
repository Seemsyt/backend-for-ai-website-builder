from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    ChatCompleteView,
    GoogleLoginView,
    LoginView,
    LogoutView,
    MeView,
    RegisterView,
    WebsiteListCreateView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="token_obtain_pair"),
    path("register/", RegisterView.as_view(), name="register"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("google/login/", GoogleLoginView.as_view(), name="google_login"),
    path("me/", MeView.as_view(), name="me"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("websites/", WebsiteListCreateView.as_view(), name="website_list_create"),
    path("chat/complete/", ChatCompleteView.as_view(), name="chat_complete"),
]
