from datetime import UTC, datetime
from typing import Any

from django import forms

from .models import Business, Service, Staff

# Slot start times travel in URLs and hidden inputs as UTC, e.g. 2026-10-12T08:00Z.
# No "+00:00" offset: a "+" in a query string decodes to a space.
START_FORMAT = "%Y-%m-%dT%H:%MZ"


def format_start(value: datetime) -> str:
    return value.astimezone(UTC).strftime(START_FORMAT)


def parse_start(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", START_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


class HoldForm(forms.Form):
    # Hidden choices are scoped to this business in __init__, so a tampered
    # id for another business's staff or service fails validation.
    service = forms.ModelChoiceField(queryset=Service.objects.none(), widget=forms.HiddenInput)
    staff = forms.ModelChoiceField(queryset=Staff.objects.none(), widget=forms.HiddenInput)
    start = forms.CharField(widget=forms.HiddenInput)
    customer_name = forms.CharField(max_length=200, label="Your name")
    customer_email = forms.EmailField(label="Email")
    customer_phone = forms.CharField(max_length=32, required=False, label="Phone (optional)")

    def __init__(self, *args: Any, business: Business, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        service_field = self.fields["service"]
        staff_field = self.fields["staff"]
        assert isinstance(service_field, forms.ModelChoiceField)
        assert isinstance(staff_field, forms.ModelChoiceField)
        service_field.queryset = business.services.filter(is_active=True)
        staff_field.queryset = business.staff.filter(is_active=True)

    def clean_start(self) -> datetime:
        parsed = parse_start(self.cleaned_data["start"])
        if parsed is None:
            raise forms.ValidationError("Please choose a time.")
        return parsed
