import uuid

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def fill_public_ids(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Booking = apps.get_model("booking", "Booking")
    for booking in Booking.objects.filter(public_id__isnull=True).only("pk"):
        booking.public_id = uuid.uuid4()
        booking.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    """Add a unique UUID in three steps.

    A single AddField(default=uuid.uuid4, unique=True) computes the default
    once and gives every existing row the same UUID, which violates the unique
    index. So: add nullable → give each existing row its own UUID → make it
    unique and non-null. (Django docs: "Migrations that add unique fields".)
    """

    dependencies = [
        ("booking", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="public_id",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(fill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="booking",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
    ]
