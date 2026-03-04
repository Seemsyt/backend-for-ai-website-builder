from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    ChatCompleteView,
    ChatThreadDetailView,
    ChatThreadListView,
    DashboardView,
    GoogleLoginView,
    LoginView,
    LogoutView,
    MeView,
    RegisterView,
    WebsiteDeployView,
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
    path("websites/<int:pk>/deploy/", WebsiteDeployView.as_view(), name="website_deploy"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("chat/threads/", ChatThreadListView.as_view(), name="chat_thread_list"),
    path("chat/threads/<int:pk>/", ChatThreadDetailView.as_view(), name="chat_thread_detail"),
    path("chat/complete/", ChatCompleteView.as_view(), name="chat_complete"),
]
