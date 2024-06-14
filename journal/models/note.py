from functools import cached_property
from typing import override

from django.db import models
from loguru import logger

from mastodon.api import delete_toot_later
from takahe.utils import Takahe

from .common import Content
from .renderers import render_text
from .shelf import ShelfMember


class Note(Content):
    title = models.TextField(blank=True, null=True, default=None)
    content = models.TextField(blank=False, null=False)
    sensitive = models.BooleanField(default=False, null=False)
    attachements = models.JSONField(default=list)

    @property
    def html(self):
        return render_text(self.content)

    @property
    def ap_object(self):
        d = {
            "id": self.absolute_url,
            "type": "Note",
            "title": self.title,
            "content": self.content,
            "sensitive": self.sensitive,
            "published": self.created_time.isoformat(),
            "updated": self.edited_time.isoformat(),
            "attributedTo": self.owner.actor_uri,
            "withRegardTo": self.item.absolute_url,
            "href": self.absolute_url,
        }
        return d

    @override
    @classmethod
    def params_from_ap_object(cls, post, obj, piece):
        return {
            "title": obj.get("title", post.summary),
            "content": obj.get("content", "").strip(),
            "sensitive": obj.get("sensitive", post.sensitive),
            # "attachements": obj.get("attachements", []),
        }

    @override
    @classmethod
    def update_by_ap_object(cls, owner, item, obj, post):
        p = super().update_by_ap_object(owner, item, obj, post)
        if (
            p
            and p.local
            and owner.user.preference.mastodon_default_repost
            and owner.user.mastodon_username
        ):
            p.sync_to_mastodon()
        return p

    @cached_property
    def shelfmember(self) -> ShelfMember | None:
        return ShelfMember.objects.filter(item=self.item, owner=self.owner).first()

    def to_mastodon_params(self):
        return {
            "spoiler_text": self.title,
            "content": self.content,
            "sensitive": self.sensitive,
            # "attachements": self.attachements,
            "reply_to_toot_url": (
                self.shelfmember.get_mastodon_repost_url() if self.shelfmember else None
            ),
        }

    def to_post_params(self):
        return {
            "summary": self.title,
            "content": self.content,
            "sensitive": self.sensitive,
            "reply_to_pk": (
                self.shelfmember.latest_post_id if self.shelfmember else None
            ),
        }
