from django.urls import path

from . import views

app_name = "booking"

urlpatterns = [
    path("b/<slug:slug>/", views.book, name="book"),
    path("b/<slug:slug>/hold/", views.create_hold, name="create_hold"),
    path("bookings/<uuid:public_id>/", views.hold_detail, name="hold_detail"),
    path("bookings/<uuid:public_id>/status/", views.hold_status, name="hold_status"),
    path("bookings/<uuid:public_id>/pay/", views.pay, name="pay"),
    path("payments/paystack/webhook/", views.paystack_webhook, name="paystack_webhook"),
]
