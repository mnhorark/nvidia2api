from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_secretaccesslog"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="requestlog",
            index=models.Index(
                fields=[
                    "created_at", "status", "model", "channel", "user_api_key",
                    "prompt_tokens", "completion_tokens", "cached_tokens",
                    "total_tokens", "duration_ms", "first_token_ms",
                ],
                name="request_log_usage_cover",
            ),
        ),
    ]
