from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Max
import random

class ChatThread(models.Model):
    owner = models.ForeignKey("User", on_delete=models.CASCADE, related_name="chat_threads")
    title = models.CharField(max_length=255, default="New chat", blank=True)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["owner", "-updated_at"]),
        ]

    def __str__(self):
        return f"{self.owner.username}: {self.title}"


class ChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"

    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    sequence = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["thread", "sequence"], name="unique_message_sequence_per_thread"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and self.sequence == 0:
            max_sequence = (
                ChatMessage.objects.filter(thread=self.thread).aggregate(max_value=Max("sequence"))["max_value"] or 0
            )
            self.sequence = max_sequence + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_role_display()} #{self.sequence} in {self.thread_id}"

class PlanFeature(models.Model):
    feature = models.CharField(max_length=300)

    def __str__(self):
        return self.feature


class Plan(models.Model):
    name = models.CharField(max_length=200)
    price = models.IntegerField()
    features = models.ManyToManyField(
        PlanFeature,
        blank=True,
        related_name="plans",
    )

    def __str__(self):
        return self.name


class User(AbstractUser):
    email = models.EmailField(unique=True)
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    avatar_url = models.URLField(blank=True)
    credits = models.PositiveIntegerField(default=20)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )

    def __str__(self):
        return self.username
class Website(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="websites")
    name = models.CharField(max_length=255 , default='untitled')
    code = models.TextField(blank=True, default="")
    domain = models.CharField(max_length=255, unique=True)
    deploy_url = models.URLField(blank=True, default="")
    deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.domain:
            self.domain = f"{self.name.lower().replace(' ', '-')}-{self.owner.username.lower()}-{self.owner.id}-{random.randint(1000, 9999)}"
        super().save(*args, **kwargs)
