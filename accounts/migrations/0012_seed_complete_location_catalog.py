from django.db import migrations

from accounts.location_data import LOCATION_DATA


def seed_complete_location_catalog(apps, schema_editor):
    State = apps.get_model('accounts', 'State')
    District = apps.get_model('accounts', 'District')

    for state_name, district_names in LOCATION_DATA.items():
        state, _ = State.objects.get_or_create(name=state_name)
        for district_name in district_names:
            District.objects.get_or_create(state=state, name=district_name)


def reverse_seed_complete_location_catalog(apps, schema_editor):
    # Keep previously seeded locations intact when reversing this data migration.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0011_scheme_geographical_scope'),
    ]

    operations = [
        migrations.RunPython(seed_complete_location_catalog, reverse_seed_complete_location_catalog),
    ]
