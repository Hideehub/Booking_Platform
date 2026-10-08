from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Enable btree_gist before any model uses it.

    The double-booking ExclusionConstraint mixes a plain equality check
    (staff) with a range overlap check inside one GiST index; btree_gist
    teaches GiST how to index plain scalar columns like a foreign key.
    """

    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [BtreeGistExtension()]
