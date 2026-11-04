# Setting up Calendar (CalDAV)
> Scope: per person · Time: ~5 min · You need: a calendar account that speaks CalDAV (Nextcloud, Fastmail, iCloud, Radicale, …) and an app password for it

Each person connects their own calendar. beepboop reads and adds events only on the calendar of the person who is speaking.

## 1. Prerequisites

- A CalDAV calendar account. Google Calendar is not CalDAV with app passwords; it arrives as its own module in v0.2.
- Two-factor sign-in turned on at your provider if it requires that before it issues app passwords (iCloud and Fastmail do).
- Your own beepboop profile with a web login, so you can approve calendar requests on your phone when your voice match is not certain.

## 2. Get credentials

Create an **app password**; never use your main account password.

1. Nextcloud: Settings › Security › Devices & sessions › Create new app password. URL: `https://<your-nextcloud>/remote.php/dav`.
2. Fastmail: Settings › Privacy & Security › Manage app passwords › New app password, access "Calendars (CalDAV)". URL: `https://caldav.fastmail.com/`.
3. iCloud: appleid.apple.com › Sign-In and Security › App-Specific Passwords. URL: `https://caldav.icloud.com/`.
4. Radicale or another self-hosted server: use your server's username and password and its base URL.

Note the username your provider expects (often your full email address).

## 3. Connect in beepboop

1. Open Integrations › caldav, section "Your connection".
2. **CalDAV URL**: the URL from step 2. A principal or calendar URL also works.
3. **Calendar**: the calendar's display name ("Personal", "Family"). Leave blank to use your first calendar that holds events.
4. **Username** and **App password**: stored encrypted; they never appear in logs, backups without the key, or anything the assistant sees.
5. Press **Test and save**. Health shows "Connected to calendar <name>" when discovery worked.

The address must use `https`. Plain `http` is accepted only for a server on your own network (`localhost`, a private IP, or a `.local` / `.lan` / `.home.arpa` name), so the app password never crosses the internet unencrypted.

## 4. Grant access

Both tools are `personal`: "what's on my calendar" and "add dentist on Friday" are allowed when your voice is a strong match; otherwise beepboop asks you to approve on your phone, and with a weak match it refuses. A spoken "yes" never counts. The Child and Guest presets cannot reach `personal` tools at all; an admin can raise a person's ceiling in People › <name> › Access.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Your calendar needs reconnecting" | Wrong or revoked app password, or none saved | Create a new app password and save it under "Your connection" |
| Health: "There is no calendar called …" | The Calendar field does not match a display name | Copy the name exactly as your provider shows it, or leave it blank |
| "redirected to another address" | Your provider moved you to another host (iCloud can do this) | Enter the full calendar URL on that host; beepboop only contacts the host in your URL |
| "must use https" | An `http://` URL on the internet | Use the provider's `https://` URL |
| Recurring events missing | The server does not expand recurrences | Events are still shown when an occurrence starts in the asked range; report your server in an issue |

## 6. Privacy

- What leaves the house: requests to the host in your CalDAV URL only (manifest `egress: setting:url`), carrying your username, app password and the date range or the event you asked to add.
- What is stored: the URL and calendar name (settings, plain text), the username and app password (encrypted secrets), and each tool call in your Activity with arguments redacted. Event contents are not stored by beepboop.
- How to delete: Integrations › caldav, section "Your connection" › Disconnect removes the secrets; deleting your profile removes everything (People › <name>).
