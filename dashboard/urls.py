from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="dashboard/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.home, name="home"),
    # Everything below is scoped to one of the owner's businesses by <slug>.
    path("<slug:slug>/", views.business_home, name="business"),
    path("<slug:slug>/services/", views.services, name="services"),
    path("<slug:slug>/services/new/", views.service_form, name="service_new"),
    path("<slug:slug>/services/<int:pk>/", views.service_form, name="service_edit"),
    path("<slug:slug>/staff/", views.staff_list, name="staff"),
    path("<slug:slug>/staff/new/", views.staff_form, name="staff_new"),
    path("<slug:slug>/staff/<int:pk>/", views.staff_detail, name="staff_detail"),
    path("<slug:slug>/staff/<int:pk>/edit/", views.staff_form, name="staff_edit"),
    path("<slug:slug>/staff/<int:pk>/shifts/", views.shift_add, name="shift_add"),
    path(
        "<slug:slug>/staff/<int:pk>/shifts/<int:shift_pk>/delete/",
        views.shift_delete,
        name="shift_delete",
    ),
    path("<slug:slug>/staff/<int:pk>/time-off/", views.time_off_add, name="time_off_add"),
    path(
        "<slug:slug>/staff/<int:pk>/time-off/<int:time_off_pk>/delete/",
        views.time_off_delete,
        name="time_off_delete",
    ),
]
