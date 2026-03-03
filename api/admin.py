from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from .models import ChatMessage, ChatThread, Plan, PlanFeature, User, Website


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Google", {"fields": ("google_sub", "avatar_url", "plan")}),
    )
    list_display = ("id", "username", "email", "google_sub", "is_staff")


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price")
    filter_horizontal = ("features",)


admin.site.register(PlanFeature)
admin.site.register(Website)


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    fields = ("sequence", "role", "content", "created_at")
    readonly_fields = ("created_at",)


@admin.register(ChatThread)
class ChatThreadAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "title", "is_archived", "updated_at")
    list_filter = ("is_archived",)
    search_fields = ("title", "owner__username", "owner__email")
    inlines = [ChatMessageInline]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "thread", "sequence", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("thread__title", "content")
