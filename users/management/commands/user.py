from time import sleep

import httpx
from django.core.management.base import BaseCommand
from tqdm import tqdm

from takahe.models import Domain, Identity
from takahe.utils import Takahe
from users.models import Preference, User
from users.models.apidentity import APIdentity


class Command(BaseCommand):
    help = "Manage users"

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="list all users")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--fix", action="store_true")
        parser.add_argument("--delete", action="store", nargs="*", help="delete user")
        parser.add_argument(
            "--integrity",
            action="store_true",
            help="check and fix integrity for missing data for user models",
        )
        parser.add_argument(
            "--remote",
            action="store_true",
            help="reset state for remote domains/users with previous connection issues",
        )
        parser.add_argument(
            "--super", action="store", nargs="*", help="list or toggle superuser"
        )
        parser.add_argument(
            "--staff", action="store", nargs="*", help="list or toggle staff"
        )
        parser.add_argument("--active", action="store", nargs="*", help="toggle active")

    def handle(self, *args, **options):
        self.verbose = options["verbose"]
        self.fix = options["fix"]
        self.users = User.objects.filter(is_active=True)
        if options["list"]:
            self.list(self.users)
        if options["integrity"]:
            self.integrity()
        if options["remote"]:
            self.check_remote()
        if options["super"] is not None:
            self.superuser(options["super"])
        if options["staff"] is not None:
            self.staff(options["staff"])
        if options["active"]:
            self.set_active(options["active"])
        if options["delete"]:
            if input("Are you sure to delete? [Y/N] ").startswith("Y"):
                self.delete(options["delete"])

    def list(self, users):
        for user in users:
            self.stdout.write(
                user.username.ljust(20)
                + str(user.date_joined.date()).ljust(12)
                + str(user.last_login.date() if user.last_login else "").ljust(12)
                + str(list(user.social_accounts.all())),
            )

    def integrity(self):
        count = 0
        self.stdout.write("Checking local users")
        for user in tqdm(User.objects.filter(is_active=True)):
            i = user.identity.takahe_identity
            if i.public_key is None:
                count += 1
                if self.fix:
                    i.generate_keypair()
            if i.inbox_uri is None:
                count += 1
                if self.fix:
                    i.ensure_uris()
            if not Preference.objects.filter(user=user).first():
                if self.fix:
                    Preference.objects.create(user=user)
                count += 1

    def check_remote(self):
        headers = {
            "Accept": "application/json,application/activity+json,application/ld+json"
        }
        with httpx.Client(timeout=0.5) as client:
            count = 0
            self.stdout.write("Checking remote domains")
            for d in tqdm(
                Domain.objects.filter(
                    local=False, blocked=False, state="connection_issue"
                )
            ):
                try:
                    response = client.get(
                        f"https://{d.domain}/.well-known/nodeinfo",
                        follow_redirects=True,
                        headers=headers,
                    )
                    if response.status_code == 200 and "json" in response.headers.get(
                        "content-type", ""
                    ):
                        count += 1
                        if self.fix:
                            Takahe.update_state(d, "outdated")
                except Exception:
                    pass
            self.stdout.write(f"{count} issues")
            count = 0
            self.stdout.write("Checking remote identities")
            for i in tqdm(
                Identity.objects.filter(
                    # public_key__isnull=True,
                    local=False,
                    restriction=0,
                    state="connection_issue",
                )
            ):
                try:
                    response = client.request(
                        "get",
                        i.actor_uri,
                        headers=headers,
                        follow_redirects=True,
                    )
                    if (
                        response.status_code == 200
                        and "json" in response.headers.get("content-type", "")
                        and "@context" in response.text
                    ):
                        Takahe.update_state(i, "outdated")
                except Exception:
                    pass
            self.stdout.write(f"{count} issues")

    def superuser(self, v):
        if v == []:
            self.stdout.write("Super users:")
            self.list(self.users.filter(is_superuser=True))
        else:
            for n in v:
                u = User.objects.get(username=n, is_active=True)
                u.is_superuser = not u.is_superuser
                u.save()
                tu = u.identity.takahe_identity.users.all().first()
                if tu:
                    tu.admin = u.is_superuser
                    tu.save()
                else:
                    self.stdout.write(f"no takahe user for {u}")
                self.stdout.write(f"update {u} superuser: {u.is_superuser}")

    def staff(self, v):
        if v == []:
            self.stdout.write("Staff users:")
            self.list(self.users.filter(is_staff=True))
        else:
            for n in v:
                u = User.objects.get(username=n, is_active=True)
                u.is_staff = not u.is_staff
                u.save()
                self.stdout.write(f"update {u} staff: {u.is_staff}")

    def set_active(self, v):
        for n in v:
            u = User.objects.get(username=n)
            u.is_active = not u.is_active
            u.save()
            self.stdout.write(f"update {u} is_active: {u.is_active}")

    def delete(self, v):
        for n in v:
            identity_id = None
            try:
                apid = APIdentity.get_by_handle(n)
                if apid.deleted:
                    self.stdout.write(f"{apid} already deleted, try anyway")
                apid.clear()
                if apid.user:
                    apid.user.clear()
                Takahe.request_delete_identity(apid.pk)
                identity_id = apid.pk
            except APIdentity.DoesNotExist:
                s = n.split("@")
                r = False
                if len(s) == 2:
                    identity = Takahe.get_identity_by_handler(s[0], s[1])
                    if identity:
                        r = Takahe.request_delete_identity(identity.pk)
                        identity_id = identity.pk
                if not r:
                    self.stdout.write(self.style.ERROR(f"identity {n} not found"))
            if identity_id:
                count_down = 10
                while count_down > 0:
                    i = Identity.objects.filter(pk=identity_id).first()
                    if i and i.state != "deleted_fanned_out":
                        self.stdout.write(f"waiting for takahe-stator...{count_down}")
                        sleep(1)
                    else:
                        break
                    count_down -= 1
                if count_down == 0:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Identity {n} was deleted, but some data in takahe has not been fully processed yet, make sure takahe-stator is running and wait a bit."
                        )
                    )
                else:
                    self.stdout.write(f"Deleted identity {n}")
