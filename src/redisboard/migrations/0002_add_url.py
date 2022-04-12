# Generated by Django 2.2.28 on 2022-04-12 13:15

from django.db import migrations
from django.db import models

import redisboard.models


class Migration(migrations.Migration):

    dependencies = [
        ('redisboard', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='redisserver',
            name='url',
            field=models.CharField(
                help_text='<a href="https://www.iana.org/assignments/uri-schemes/prov/redis">IANA-compliant</a> URL. Examples: <pre>'
                'redis://[[username]:[password]]@localhost:6379/0\n'
                'rediss://[[username]:[password]]@localhost:6379/0\n'
                'unix://[[username]:[password]]@/path/to/socket.sock?db=0</pre>',
                max_length=250,
                null=True,
                validators=[redisboard.models.validate_url],
                verbose_name='URL',
            ),
        ),
    ]
