# 777 Discord Bot

A starter Discord bot for the **777 Roblox friend group**.

## Included features

- `/ping`
- `/about`
- Welcome messages
- Goodbye messages
- Render-compatible health endpoint
- Automatic slash-command syncing

## 1. Create the Discord application

1. Open the Discord Developer Portal.
2. Create a new application called **777**.
3. Open the **Bot** section and create the bot.
4. Copy the token. Never upload the token to GitHub.
5. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**

## 2. Invite the bot

In the Developer Portal, open **OAuth2 → URL Generator**.

Select:

- `bot`
- `applications.commands`

Recommended bot permissions:

- View Channels
- Send Messages
- Embed Links
- Read Message History

Open the generated URL and invite 777 to the server.

## 3. Set the welcome channel

Enable Discord Developer Mode:

**User Settings → Advanced → Developer Mode**

Right-click the welcome channel and choose **Copy Channel ID**.

You will add this number to Render as `WELCOME_CHANNEL_ID`.

If you leave it blank, the bot tries to use the server's system channel.

## 4. Upload to GitHub

Create a new GitHub repository and upload all files from this folder.

Do not upload a real `.env` file or your bot token.

## 5. Deploy on Render

### Using the included Blueprint

1. In Render, choose **New → Blueprint**.
2. Connect your GitHub repository.
3. Render will read `render.yaml`.
4. Enter these environment variables:
   - `DISCORD_TOKEN`
   - `WELCOME_CHANNEL_ID`
5. Deploy the service.

### Manual setup

Create a Python Web Service with:

- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Health check path: `/health`

Add:

- `DISCORD_TOKEN`
- `WELCOME_CHANNEL_ID`

## Testing

Once deployed:

- The Render logs should show `Logged in as 777`.
- Run `/ping` in Discord.
- Run `/about`.
- Test joins with another account if possible.

## Troubleshooting

### Welcome messages do not appear

Check that:

- Server Members Intent is enabled in the Developer Portal.
- The bot can view and send messages in the welcome channel.
- `WELCOME_CHANNEL_ID` contains only the numeric channel ID.
- You restarted or redeployed the bot after changing settings.

### Slash commands do not appear

Wait a minute, then restart Discord. Also confirm the invite included the
`applications.commands` scope.

### Bot token was exposed

Reset it immediately in the Discord Developer Portal and update the Render
environment variable.
