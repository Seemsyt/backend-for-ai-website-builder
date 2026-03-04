from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import ChatMessage, ChatThread, PlanFeature, Website

User = get_user_model()


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "avatar_url",
            "credits",
        )


class EmailOrUsernameTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        username = attrs.get("username")
        if username and "@" in username:
            user = User.objects.filter(email__iexact=username).only("username").first()
            if user:
                attrs["username"] = user.username

        data = super().validate(attrs)
        data["user"] = UserProfileSerializer(self.user).data
        return data
class PlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ("id", "feature")
class WebsiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Website
        fields = ("id", "name", "code", "domain", "deploy_url", "deployed_at", "created_at", "updated_at")


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ("id", "thread", "role", "content", "sequence", "metadata", "created_at")
        read_only_fields = ("id", "created_at")


class ChatThreadSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)

    class Meta:
        model = ChatThread
        fields = ("id", "owner", "title", "is_archived", "created_at", "updated_at", "messages")
        read_only_fields = ("id", "owner", "created_at", "updated_at", "messages")


class ChatThreadListItemSerializer(serializers.ModelSerializer):
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ChatThread
        fields = ("id", "title", "updated_at", "last_message")

    def get_last_message(self, obj):
        message = obj.messages.order_by("-sequence", "-created_at").first()
        if not message:
            return ""
        return message.content[:120]
