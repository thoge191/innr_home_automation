# Cloud client identity (`[cloud.client]`)

## Why this exists
The INNR cloud API is not documented for third parties. The official INNR app identifies itself to the cloud with a set
of values: an application identifier, a user agent, a package name, version numbers, a device model and name, and a web
origin. The cloud only accepts requests that carry values that look like the official app. This service therefore needs
such values to log in and to send commands.

**This project does not ship them.** You supply them in your own configuration. That keeps the code and the repository free
of another party's application identifiers, and it makes it your explicit decision how your installation identifies itself.
Without them the service refuses to start (`cloud.client is not configured`).

## Legal and risk notes (not legal advice)
- The service is meant for **your own INNR account and your own devices**. It does not bypass authentication, rate limits or
  other access controls; it needs your login. Do not use it for accounts or devices that are not yours.
- INNR's terms of service may forbid non-official clients or automated access. Read them for your market. Your account could
  be limited or blocked, and the cloud may change at any time. Use at your own risk.
- The maintainers of this project do not provide the values and cannot help you obtain them.

## The keys
All keys are required. Values are printable ASCII, at most 300 characters; a table with every value empty counts as "not
configured", a half-filled table is an error that names the missing keys.

| Key | What it is | Sent as |
|---|---|---|
| `app_id` | Application identifier of the app (a long number in string form) | header `appId` |
| `user_agent` | The browser/user-agent string of the app's web view | header `User-Agent` |
| `package_name` | The app's package name | header `X-Requested-With`, login field `packName` |
| `app_version` | Native app version | login field `nativeVersion` |
| `web_version` | Version of the app's web layer | header `webVersion`, login and token-refresh fields |
| `build_number` | Build number of the app (whole number, not quoted) | login field `buildNumber` |
| `os_version` | Operating system version string of the reported device | login field `osVersion` |
| `web_view_version` | Version of the web view component | login field `webViewVersion` |
| `device_model` | Device model string of the reported device | login field `terminalModelName` |
| `device_name` | Device name of the reported device | login field `terminalName` |
| `origin` | Web origin the app's web layer runs under | headers `Origin` and `Referer` |

## Where the values come from
Other open-source projects that talk to the same cloud publish what they send. The Homey app credited in [NOTICE](../NOTICE)
keeps these values in its source file `lib/api.js`
([homey-innr-bridge](https://github.com/smarthomesven/homey-innr-bridge)); take the values that match the app version you
rely on from a source you trust. Do not invent values: the cloud rejects unknown ones.

## How to configure
**Recommended:** keep the values in a local file that git ignores, and let the deploy scripts do the rest.

```bash
cp deploy/client.toml.example deploy/client.toml      # git-ignored; fill in every value
deploy/deploy-local.sh sha-<8>                         # merges it into the Mac config on every run
deploy/deploy-nas.sh deploy sha-<8>                    # merges it into the NAS config before anything is started
```

The merge (`deploy/merge-client-profile.sh`) replaces only the `[cloud.client]` table of `config.toml` and refuses a profile
file that contains any other table, so this file cannot change anything else. It is idempotent.

**Alternative:** edit the `[cloud.client]` table of `config.toml` directly.

## Checking
- `innr-autooff check-config` prints a warning while the table is empty; `innr-autooff run` refuses to start.
- A repository test fails if the known values of the official app ever appear in a tracked file, so they cannot slip into a
  commit by accident. `deploy/client.toml` is in `.gitignore`.

## When the cloud changes
If the official app is updated and the cloud starts rejecting logins (`cloud_error call=devices kind_of=auth` in
`signals.log`, HTTP errors in the reason), update the version values (`app_version`, `web_version`, `build_number`,
`web_view_version`) in `deploy/client.toml` and deploy again.
