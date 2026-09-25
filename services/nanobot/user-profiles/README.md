# User profiles

One file per household member, named after their id in `config/home-stack.yml`
— `user1.md`, `user2.md` and so on. The assistant reads its own member's file
as `USER.md` in its workspace.

This directory ships with a template and nothing else. It used to ship with a
real household's profiles in it: full legal names, birthdates, who is whose
sister, which child needs simpler language. That is exactly the material an
assistant is useful for holding and exactly the material nobody else's install
should arrive carrying, so the package ships the shape and none of the content.

## You do not write these files here

**Household members are managed on the admin page and nowhere else.** The
deployer builds one profile per configured member and writes it to
`{paths.config}/nanobot/user-profiles/<id>.md` on the target — the checkout's
copy of this directory only carries the template, and a `user1.md` written
beside it is never read.

From there the container seeds it into the member's workspace **once**:

```sh
[ -f "/mnt/$u/workspace/USER.md" ] || cp "$f" "/mnt/$u/workspace/USER.md"
```

Once only, because memory consolidation writes to `USER.md` — copying it again
on every deploy would throw away everything the assistant has learned about
that person. So after the first deploy the file belongs to the assistant and to
the person editing it from the app, not to this directory.

`user-profile.template.md` is the shape a generated profile takes. Nothing here
is required: a member with an empty profile means the assistant simply knows
nothing about them yet.
