---
layout: default
---

# Outlook / Office 365 email accounts

Microsoft turns off mailbox-password login for Outlook and Microsoft 365.
Odysseus connects those mailboxes with Microsoft sign-in (OAuth2 / XOAUTH2)
on the same IMAP and SMTP path Gmail already uses. Background polling and the
`email_received` event keep working. A message is not sent until you approve
it.

Calendar and contacts are a separate, optional Microsoft 365 connection.
The repository file docs/PHASE2_SETUP.md has the Entra registration steps,
including what to do when a work tenant blocks sign-in.

## Add a mailbox

1. Put `MICROSOFT_OAUTH_CLIENT_ID` and `MICROSOFT_OAUTH_CLIENT_SECRET` in the
   environment and restart Odysseus. Register the app in Microsoft Entra first.
   The redirect URI must be
   `http://localhost:7011/api/email/oauth/microsoft/callback` for a default
   Docker install (port 7011 on the host, port 7000 inside the container).
2. Settings → Integrations → Email → Add account → Outlook / Office 365.
3. Choose **Work or school** or paste the work tenant id. Personal
   Outlook.com uses **Personal**.
4. Click **Connect with Microsoft** and sign in as the mailbox you are adding.
5. Leave the username blank, or type the same address you will sign in with.
   A different username is rejected so one login cannot be attached to
   another mailbox.

A normal password still fails. The error tells you to use Microsoft sign-in
instead of echoing Microsoft's `535` code.

## Several accounts

Add one email account per mailbox and run Connect with Microsoft on each.
A work mailbox and a personal mailbox can use different tenant choices.
Graph calendar and contacts use one MCP server row per login
(`MS365_MCP_SERVERS`), described in the Phase 2 notes.

## What stays off by default

Graph tools that send, reply, forward, delete, or move mail are hidden.
Drafts stay available. Sending goes through **draft_email** / **send_email**
and the existing approval card, then SMTP with the Microsoft token.
