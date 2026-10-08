from django.contrib import admin
from django.http import HttpRequest

from .models import Booking, Business, Payment, Service, Staff, TimeOff, WorkingHours


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


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["customer_name", "staff", "service", "start_at", "end_at", "status"]
    list_filter = ["status", "staff"]
    date_hierarchy = "start_at"
    search_fields = ["customer_name", "customer_email", "customer_phone"]
    list_select_related = ["staff", "service"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Read-only: payment records come from Paystack and must not be hand-edited."""

    list_display = ["reference", "booking", "amount_minor", "status", "paid_at"]
    list_filter = ["status"]
    search_fields = ["reference"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Payment | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Payment | None = None) -> bool:
        return False
