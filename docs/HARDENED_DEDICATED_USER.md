# Hardened deployment with a dedicated non-admin macOS user

This is an **advanced containment option**, not the default Mac MCP setup and not a sandbox. Running Mac MCP as a separate standard macOS user can reduce the files, credentials, browser profiles, Keychain items, and privacy permissions that are reachable if an agent or one of its tools is compromised. It does not provide VM/container isolation, and it does not make loopback TCP a same-user security boundary.

Use this model only if you are comfortable operating two macOS accounts and accepting the GUI-automation trade-offs described below.

## Threat model and topology

A typical hardened setup is:

```text
Primary macOS user / MCP client
        |
        |  http://127.0.0.1:<port>/mcp
        |  Authorization: Bearer <MCP_API_KEY>
        v
Dedicated standard user: macmcp
  - Mac MCP runtime and state
  - its own home directory / Keychain
  - its own Safari / Chrome profiles
  - its own TCC grants
  - optional explicitly shared folder only
```

Loopback networking is shared by the Mac, so a client running as the primary user can connect to a service owned by another local user. **Authentication is still required** because `127.0.0.1` identifies the machine, not the Unix user making the request. Keep:

```dotenv
MCP_ALLOW_NO_AUTH=false
MCP_API_KEY=<long-random-secret>
```

Keep the server on the default loopback host (`127.0.0.1`). Do not change `MAC_MCP_HOST` to `0.0.0.0` or `::` merely to communicate between two local users; that is unnecessary and broadens the network exposure. If you intentionally expose `/mcp` through a tunnel, keep MCP authentication enabled there as well.

The dashboard has a **different** credential stored by default at `~/.mac-mcp/dashboard-token` with owner-only permissions. In a dedicated-user deployment that file belongs to the dedicated account. Do not copy it into the primary user's home just to make the primary user's menu app work. Prefer one of these models:

- use `/mcp` from the primary account with the global MCP API key, and do not expose the dashboard across accounts; or
- run the Mac MCP menu app/dashboard inside the dedicated user's GUI session, where its dashboard token naturally belongs.

## 1. Create a standard account

Prefer **System Settings → Users & Groups → Add User → Standard**. A standard user can manage its own settings but cannot add other users or make the same system-wide changes as an administrator.

For administrators who deliberately use the CLI, macOS also exposes `sysadminctl`. Do not put a password on the command line; `-password -` requests it interactively:

```bash
sudo sysadminctl -addUser macmcp -fullName "Mac MCP Service" -password -
```

Do **not** add `-admin`, do not add the account to the `admin` group, and do not configure passwordless sudo for Mac MCP. The objective is to leave privilege escalation outside the service boundary.

## 2. Install and run Mac MCP as that user

Log in to the `macmcp` account normally, open Terminal there, and install Mac MCP using the normal installation instructions. Source, runtime, state, virtualenv, `.env`, menu app, provider configuration, and local logs should all be owned by that account.

Verify ownership from the dedicated account:

```bash
id
ls -ld ~/Projects/mac-mcp ~/mac-mcp ~/.mac-mcp
ls -l ~/mac-mcp/mcp_server/.env
```

The runtime `.env` should remain owner-readable only (normally mode `0600`). Start the daemon **as the dedicated user**, not with `sudo` from the primary account. The stock CLI defaults to `127.0.0.1`.

For terminal/files/search/HTTP-only use, a GUI login is not inherently required. For Safari, Chrome, Accessibility, screenshots, AppleScript, microphone, or other desktop integration, keep reading: those features depend on the dedicated user's GUI/TCC context.

## 3. Connect from the primary account

From the primary account, test only the non-sensitive health endpoint first:

```bash
curl --fail http://127.0.0.1:8000/health
```

Then configure the MCP client with the dedicated runtime's **global MCP API key** and the local endpoint:

```text
http://127.0.0.1:8000/mcp
Authorization: Bearer <MCP_API_KEY>
```

The port may differ if you configured `MAC_MCP_PORT`.

Do not solve cross-user connectivity by weakening `MCP_ALLOW_NO_AUTH`, by sharing the entire dedicated home directory, or by making the dashboard token world-readable.

## 4. Share only an explicit filesystem tree

Do not share either user's whole home directory. In particular, do not ACL-share `.ssh`, `.config`, browser profile directories, Keychain data, `~/Library`, the Mac MCP `.env`, or `~/.mac-mcp`.

A dedicated exchange directory under `/Users/Shared` is easier to audit. The example below gives both named users access only to that tree and makes the ACL inheritable. Substitute real short usernames before running it:

```bash
PRIMARY_USER="your-primary-short-name"
MCP_USER="macmcp"
SHARED="/Users/Shared/MacMCPExchange"

sudo install -d -o "$MCP_USER" -g wheel -m 0700 "$SHARED"

for USER in "$PRIMARY_USER" "$MCP_USER"; do
  # Directory rights, inherited by nested directories.
  sudo chmod +a "user:${USER} allow list,search,add_file,add_subdirectory,delete_child,readattr,writeattr,readextattr,writeextattr,readsecurity,directory_inherit" "$SHARED"

  # File rights. only_inherit keeps this ACE from broadening the root directory
  # while file_inherit + directory_inherit carries it through nested trees.
  sudo chmod +a "user:${USER} allow read,write,append,readattr,writeattr,readextattr,writeextattr,readsecurity,file_inherit,directory_inherit,only_inherit" "$SHARED"
done

ls -led "$SHARED"
```

This deliberately does **not** grant `chown`, `writesecurity`, or executable-file permission.

To remove the ACL without deleting any data:

```bash
sudo chmod -RN "/Users/Shared/MacMCPExchange"
sudo chown macmcp:wheel "/Users/Shared/MacMCPExchange"
sudo chmod 0700 "/Users/Shared/MacMCPExchange"
```

Review the contents before deciding whether to remove the directory itself. The ACL syntax and nested file inheritance shown above were validated on macOS with a disposable `/tmp` tree; removal with `chmod -RN` was also validated. The example was not applied to `/Users/Shared` on the user's machine.

## 5. Understand the GUI-session boundary

A dedicated macOS account does **not** let Mac MCP reach through into another user's desktop session.

- Safari/Chrome automation targets browser instances available to the user/session running Mac MCP. It should not be expected to control the primary user's logged-in Safari tabs, cookies, or browser profile.
- Accessibility permission is granted through **Privacy & Security → Accessibility** to applications in the relevant user's environment.
- Apple Events / app-to-app control is governed through **Privacy & Security → Automation**.
- Screen capture features may require Screen Recording permission.
- Files & Folders / Full Disk Access controls can further restrict protected locations. Do not grant Full Disk Access merely to make a dedicated-user deployment convenient.
- TCC prompts and grants should be completed while logged in as the dedicated user. Treat them as part of that account's security boundary.

Fast User Switching and background login sessions can change which GUI resources are available. Mac MCP does not promise reliable Safari/Accessibility automation against a non-active GUI session. If GUI automation is required, log in to the dedicated account, grant only the required permissions there, and validate the exact workflow in that session.

## 6. Tool behavior under a standard user

| Tool class | Expected dedicated-user behavior |
| --- | --- |
| File read/write/search | Limited by Unix mode/ACL plus macOS privacy controls. Other users' private homes should remain inaccessible unless explicitly shared. |
| Shell / terminal | Runs with the dedicated user's uid. Commands requiring root/admin rights fail or request elevation outside Mac MCP. Do not configure NOPASSWD sudo. |
| Process inspection/control | Process visibility is OS-dependent; signalling or controlling processes remains subject to macOS/Unix permissions. |
| HTTP/network | Normally works as a standard user. Keep Mac MCP authentication enabled even on loopback. |
| Safari / Chrome automation | Uses browsers and profiles in the dedicated user's GUI session, not the primary user's browser session. |
| AppleScript / Accessibility / UI control | Requires the dedicated user's GUI session and the appropriate TCC grants. It cannot be treated as a cross-user desktop-control mechanism. |
| Screenshot / screen interaction | Requires the appropriate screen/privacy permission for the dedicated user's context. |
| Keychain-backed credentials | Belong to and are unlocked in the dedicated user's Keychain context; do not copy the primary user's Keychain or secrets wholesale. |
| System/user management, protected system changes | Expected to fail without administrator authorization. This is part of the containment benefit. |

## 7. Two-user validation matrix

Run this matrix on a test Mac or during a controlled maintenance window before depending on the topology. Do not infer GUI isolation from a successful TCP test.

| Test | Expected result | Current verification status |
| --- | --- | --- |
| Dedicated standard user starts Mac MCP on `127.0.0.1:<port>` | Server starts without admin/root | Manual two-user validation required |
| Primary user curls `/health` over loopback | HTTP 200 | Manual two-user validation required |
| Primary user calls `/mcp` with valid Bearer key | Accepted | Manual two-user validation required |
| Primary user calls `/mcp` without/wrong key | Rejected | Covered by Mac MCP auth regression tests; repeat in two-user setup |
| Primary user reads dedicated user's private home | Denied unless macOS permissions were deliberately weakened | Manual two-user validation required |
| Both users read/write only `MacMCPExchange` | Allowed according to ACL | ACL grammar, inheritance, and rollback locally validated; cross-user execution required |
| New nested file inherits exchange ACL | Named-user file ACE is inherited | Locally validated with disposable tree |
| `chmod -RN` rollback | ACL removed without deleting contents | Locally validated with disposable tree |
| Mac MCP controls dedicated user's Safari after dedicated-user TCC grants | Works in that GUI context | Manual GUI validation required |
| Mac MCP controls primary user's Safari from dedicated account | Must **not** be assumed/supported | Manual negative validation recommended |
| Accessibility/Automation grant only in primary user's settings | Must not be treated as permission for dedicated-user Mac MCP | Manual TCC validation required |
| Root/admin-only command from Mac MCP | Fails or requires external admin authorization | Manual validation recommended; do not add NOPASSWD sudo |
| Primary-user menu app reads dedicated dashboard token | Should fail by normal home/file permissions | Manual two-user validation required; do not copy the token |

Record the macOS version, active GUI user, Mac MCP commit, browser, and granted TCC categories when validating. GUI/TCC behavior is exactly the area where a generic “service account” mental model is misleading.

## 8. Operational checklist

Before calling the deployment hardened:

- dedicated account is **Standard**, not Administrator;
- `MCP_ALLOW_NO_AUTH=false` and the MCP API key is unique;
- daemon binds to `127.0.0.1` unless remote exposure is explicitly required;
- no passwordless sudo is configured for the account or Mac MCP binaries;
- only explicit exchange paths are ACL-shared;
- primary home, `.ssh`, browser profiles, Keychain files, `.env`, and `~/.mac-mcp` are not shared;
- dedicated-user TCC grants are the minimum needed for the enabled tools;
- GUI automation has been tested in the dedicated user's actual GUI session;
- dashboard/menu app remains in the dedicated user unless you have designed a separate, explicit dashboard-access boundary;
- update/backup procedures are run as the dedicated user and preserve ownership/modes.

## What this model does not provide

A dedicated standard account reduces blast radius through normal macOS account, filesystem, credential, and privacy boundaries. It is **not** a complete sandbox, VM, container, or formal cross-user mediation layer. A kernel/system compromise, administrator authorization, deliberately shared secrets, permissive ACLs, or over-broad TCC grants can defeat the intended containment.

If you need stronger isolation than this model provides, use a separate Mac/VM or another isolation technology designed as a security boundary rather than stretching the dedicated-user setup beyond its purpose.

## Apple references

- Users & Groups / standard accounts: https://support.apple.com/guide/mac-help/change-users-groups-settings-mtusr001/mac
- Accessibility privacy permission: https://support.apple.com/guide/mac-help/mh43185/mac
- Automation / controlling other apps: https://support.apple.com/guide/mac-help/mchl07817563/mac
- macOS app access to protected files: https://support.apple.com/guide/security/secddd1d86a6/web
- Sharing files between users on one Mac: https://support.apple.com/guide/mac-help/mchlp1122/mac
