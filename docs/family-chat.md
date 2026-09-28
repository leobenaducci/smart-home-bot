# Family chat

Messages between the household's members, one to one or to a group, built
mostly for emergencies: a message is meant to reach the person, whatever state
their phone is in.

## What a recipient gets

- **Every message vibrates** the phone -- on the alarm stream, which is what
  Do Not Disturb and silent mode let through -- until it is opened or snoozed.
- **Urgent** (the sender ticks it) **rings as well**, at alarm volume.
- **Snooze** is one button, five minutes, and then it alerts again. In an
  emergency a snooze must not be a way for a message to disappear.
- **Seen** is opening the conversation, on any of the person's devices; the
  alert stops on all of them.

The app does the alerting (`FamilyAlert`), not the notification channel, and
needs Do Not Disturb access for the channel's bypass to apply -- it asks once.

## Groups

`Family` (every active member) and `Parents` (members with the Parents box)
follow the members and are the same two groups the phones' ntfy topics are.
The household adds its own on the admin page's Hogar page. Phone numbers are
on each person's profile there.

## Delivery, and SMS when there is no data

| Situation | What happens |
|---|---|
| Both online | Portal → `family_msg` push → the app confirms delivery, alerts |
| Push not confirmed | Pushed again each minute (up to ten times) |
| Still not delivered after 3 min | `family_sms` to the **sender's** phone, which texts it from its SIM |
| Sender has no data | The sender's app texts every recipient directly, and syncs to the portal later |
| Recipient has only SMS | Their app recognises the family text and alerts the same way |

Each APK carries the family directory (names, numbers, groups), written into
the build by the admin page (`app_family_directory` in the deployer) and
refreshed from `/family-chat/api/directory` whenever the phone is online. It
is never in this repository: the build writes it into its staging copy only.

The app's own screen for it (`FamilyActivity`) is a second launcher entry,
**Familia Chat**, with its own icon: the chat page's Family panel needs the
portal, and this screen opens with no data at all.

## The SMS format

The protocol between two phones that may share nothing else at that moment,
so it is fixed (`FamilySms`, tested in `FamilySmsFormatTest`):

    [Alfred] <conversation> | <from>: <text> #fc:<key>
    [Alfred URGENTE] ...

`<key>` is `s<id>` for a message the portal asked a phone to text, or the
sender's client id for one sent with no data -- the same id the portal uses to
keep an SMS-first message and its later sync one message. A family-format text
is only believed from a number in the directory.

## Where it lives

- Portal: `/family-chat/api/*` and the worker in `services/home-core/local/app.py`
  (`family_chat.db`); the Family panel in `templates/chat.html`.
- App: `services/proxy/android/.../family/`.
- Tests: `test_family_chat.py` (portal), `FamilySmsFormatTest` (app),
  `deploy/test_deploy.py` (groups and the app's directory).
