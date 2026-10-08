from typing import Any

from django import forms
from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from .models import Booking, Business, Payment, Service, Staff, TimeOff, WorkingHours
from .services import expire_stale_holds, mark_payments_refunded


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 0


class StaffInline(admin.TabularInline):
    model = Staff
    extra = 0
    fields = ["name", "email", "is_active"]
    show_change_link = True


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "timezone", "owner"]
    prepopulated_fields = {"slug": ["name"]}
    search_fields = ["name", "slug"]
    inlines = [ServiceInline, StaffInline]


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "business", "duration", "price_minor", "deposit_minor", "is_active"]
    list_filter = ["business", "is_active"]


class WorkingHoursInline(admin.TabularInline):
    model = WorkingHours
    extra = 0


class TimeOffInline(admin.TabularInline):
    model = TimeOff
    extra = 0


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ["name", "business", "is_active"]
    list_filter = ["business", "is_active"]
    filter_horizontal = ["services"]
    inlines = [WorkingHoursInline, TimeOffInline]


class BookingAdminForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = [
            "staff",
            "service",
            "customer_name",
            "customer_email",
            "customer_phone",
            "start_at",
            "end_at",
            "status",
            "hold_expires_at",
            "deposit_minor",
        ]

    def clean(self) -> dict[str, Any] | None:
        # The overlap constraint is checked during validation, so lapsed holds
        # must be expired *before* it runs or they'd block this booking. A write
        # inside clean() is unusual, but expiring lapsed holds is always correct
        # and repeating it changes nothing.
        staff = self.cleaned_data.get("staff")
        if staff is not None:
            expire_stale_holds(staff, timezone.now())
        return super().clean()


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    form = BookingAdminForm
    list_display = ["customer_name", "staff", "service", "start_at", "end_at", "status"]
    list_filter = ["status", "staff"]
    date_hierarchy = "start_at"
    search_fields = ["customer_name", "customer_email", "customer_phone"]
    list_select_related = ["staff", "service"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Read-only: payment records come from Paystack and must not be hand-edited."""

    list_display = ["reference", "booking", "amount_minor", "status", "paid_at", "refunded_at"]
    list_filter = ["status"]
    search_fields = ["reference"]
    actions = ["mark_refunded"]

    @admin.action(
        description="Mark selected as refunded (after refunding in Paystack)",
        permissions=["mark_refunded"],
    )
    def mark_refunded(self, request: HttpRequest, queryset: QuerySet[Payment]) -> None:
        updated = mark_payments_refunded(queryset, now=timezone.now())
        for payment in updated:
            # Shows in the payment's admin History: who recorded the refund, when.
            self.log_change(request, payment, "Marked as refunded")
        skipped = queryset.count() - len(updated)
        self.message_user(request, f"Marked {len(updated)} payment(s) as refunded.")
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} payment(s) that weren't refund due.",
                level=messages.WARNING,
            )

    def has_mark_refunded_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("booking.mark_payment_refunded")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Payment | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Payment | None = None) -> bool:
        return False
