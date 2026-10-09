from datetime import timedelta
from decimal import Decimal
from typing import Any

from django import forms

from booking.models import Business, Service, Staff, TimeOff, WorkingHours

INPUT = (
    "mt-1.5 block w-full rounded-xl border border-linen bg-ivory px-4 py-2.5 text-espresso "
    "focus:border-gold-deep focus:bg-white focus:outline-none focus:ring-4 focus:ring-gold-soft"
)
CHECKBOX = "h-4 w-4 rounded border-linen text-gold-deep accent-gold-deep"


class StyledForm(forms.BaseForm):
    """Give every widget the dashboard's input styling."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            is_check = isinstance(widget, forms.CheckboxInput | forms.CheckboxSelectMultiple)
            widget.attrs.setdefault("class", CHECKBOX if is_check else INPUT)


def to_minor(amount: Decimal) -> int:
    """Naira (2 d.p., already validated) to kobo, exactly: no float rounding."""
    return int(amount * 100)


def to_major(amount_minor: int) -> Decimal:
    return Decimal(amount_minor) / 100


class ServiceForm(StyledForm, forms.ModelForm):  # type: ignore[type-arg]
    """Owners think in minutes and naira; the model stores a timedelta and kobo."""

    duration_minutes = forms.IntegerField(
        label="Duration (minutes)", min_value=5, max_value=12 * 60, step_size=5
    )
    price = forms.DecimalField(label="Price", min_value=0, max_digits=12, decimal_places=2)
    deposit = forms.DecimalField(
        label="Deposit",
        min_value=0,
        max_digits=12,
        decimal_places=2,
        help_text="Charged when booking. 0 means bookings are confirmed without payment.",
    )

    class Meta:
        model = Service
        fields = ["name", "is_active"]
        labels = {"is_active": "Customers can book this service"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["duration_minutes"].initial = int(
                self.instance.duration.total_seconds() // 60
            )
            self.fields["price"].initial = to_major(self.instance.price_minor)
            self.fields["deposit"].initial = to_major(self.instance.deposit_minor)
        self.order_fields(["name", "duration_minutes", "price", "deposit", "is_active"])

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        price, deposit = data.get("price"), data.get("deposit")
        if price is not None and deposit is not None and deposit > price:
            self.add_error("deposit", "The deposit can't be more than the price.")
        return data

    def save(self, commit: bool = True) -> Service:
        service: Service = super().save(commit=False)
        service.duration = timedelta(minutes=self.cleaned_data["duration_minutes"])
        service.price_minor = to_minor(self.cleaned_data["price"])
        service.deposit_minor = to_minor(self.cleaned_data["deposit"])
        if commit:
            service.save()
        return service


class StaffForm(StyledForm, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Staff
        fields = ["name", "email", "services", "is_active"]
        widgets = {"services": forms.CheckboxSelectMultiple}
        labels = {"is_active": "Taking bookings", "services": "Services they offer"}

    def __init__(self, *args: Any, business: Business, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Only this business's services: a tampered id from another business
        # fails validation instead of linking across businesses.
        services = self.fields["services"]
        assert isinstance(services, forms.ModelMultipleChoiceField)
        services.queryset = business.services.order_by("name")
        services.required = False


class WorkingHoursForm(StyledForm, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = WorkingHours
        fields = ["weekday", "start_time", "end_time"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }
        labels = {"start_time": "Starts", "end_time": "Ends"}

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start, end = data.get("start_time"), data.get("end_time")
        if start and end and end <= start:
            self.add_error("end_time", "The shift must end after it starts.")
        return data


class TimeOffForm(StyledForm, forms.ModelForm):  # type: ignore[type-arg]
    """Date-times are entered in the business's local time: owner views run
    inside timezone.override(), so Django converts them to UTC on save."""

    class Meta:
        model = TimeOff
        fields = ["start_at", "end_at", "reason"]
        widgets = {
            "start_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "end_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }
        labels = {"start_at": "From", "end_at": "Until"}

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start, end = data.get("start_at"), data.get("end_at")
        if start and end and end <= start:
            self.add_error("end_at", "Time off must end after it starts.")
        return data
