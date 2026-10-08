from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Project user model.

    Empty for now; it exists so fields can be added later without the painful
    mid-project swap away from django.contrib.auth.models.User.
    """
