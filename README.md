# SM Sentinel

SM Sentinel (Staking Modules Sentinel) is a Telegram bot that sends notifications for Lido staking module Node Operator events.
It is designed as a multi-module watcher and currently supports both the Community Staking Module (CSM) and Curated Module deployments.

The project was originally created as CSM Sentinel by [@skhomuti](https://github.com/skhomuti), a member of the Lido Protocol community,
to simplify the process of [subscribing to the important events for CSM](https://docs.lido.fi/staking-modules/csm/guides/events/).
It now lives at [lidofinance/sm-sentinel](https://github.com/lidofinance/sm-sentinel) and covers multiple Lido staking modules.
You can either [run the bot yourself](https://github.com/lidofinance/sm-sentinel?tab=readme-ov-file#running-your-own-instance)
or use a [community-supported public instance](https://github.com/lidofinance/sm-sentinel?tab=readme-ov-file#public-instances), depending on your privacy preferences.

## Module support

The bot discovers the module type from `MODULE_ADDRESS` on startup and wires the matching notification adapter automatically:

- **Community Staking Module (CSM):** supports CSM v2 and v3 contracts, including automatic v3 switching after the module is initialized.
- **Curated Module:** supports Curated Module events, MetaRegistry-based Node Operator labels, operator group notifications, and Curated-specific follow/unfollow buttons.

For CSM, subscriptions are managed by Node Operator ID. For Curated, the bot also shows Node Operator labels from the MetaRegistry when they are available.

## Public Instances
These bots are owned by [@skhomuti](https://t.me/skhomuti):

- Community Staking Module: [Ethereum](https://t.me/CSMSentinel_bot) and [Hoodi](https://t.me/CSMSentinelHoodi_bot)
- Curated Module: [Hoodi](https://t.me/CMSentinelHoodi_bot)

The [Holesky](https://t.me/CSMSentinelHolesky_bot) instance is no longer supported. 

Please note that no guarantee is given for the availability of the bot.
Also consider privacy concerns when using a public instance.

## Running your own instance 

First, you need to create a bot on Telegram. You can do this by talking to the [BotFather](https://t.me/botfather).

Then, create a `.env` by copying the sample matching the module and network you want to monitor:

- CSM: `.env.sample.ethereum` or `.env.sample.hoodi`
- Curated Module: `.env.sample.curated.ethereum` or `.env.sample.curated.hoodi`

Fill in the required fields:
- `TOKEN`: The token you received from the BotFather
- `WEB3_SOCKET_PROVIDERS`: A comma-separated list of websocket providers. The first URL is
the primary provider; SM Sentinel switches to the next healthy URL when the active
connection fails. `WEB3_SOCKET_PROVIDER` remains supported as an equivalent legacy alias;
`WEB3_SOCKET_PROVIDERS` takes precedence when both are set.
Preferably, use your own local node, for example the execution node you already run for validators.
But it is also possible to use a public node of any web3 providers.
- `MODULE_ADDRESS`: The staking module address to monitor. Use the CSM address for a CSM instance or the Curated Module address for a Curated instance.
- `MODULE_UI_URL`: Optional URL used in notification links. Use the matching CSM or Curated Module UI.

All other fields are pre-filled for the selected module and network. Dependent contract addresses, module type, staking module ID, and MetaRegistry address are discovered on startup.

SM Sentinel supports two dotenv files:

1. the application environment: the standard `.env` discovered by `python-dotenv`, or the file
   referenced by `ENV_FILE_PATH`;
2. an optional external secret bundle referenced by `SECRETS_FILE_PATH`.

Existing process environment variables take precedence over the application environment file.
Secret bundle values always take precedence. When `SECRETS_FILE_PATH` is configured, its file must
include a positive integer `SECRET_VERSION`. Sentinel checks that version periodically and shuts
down gracefully when it changes so the process supervisor can restart it with the new secrets.
Local setups that provide all values through `.env` or process environment do not need either file
path.

Run SM Sentinel using Docker compose:

```bash
docker compose up -d
```

Or using Docker:

```bash
docker build -t sm-sentinel .
docker volume create sm-sentinel-persistent

docker run -d --env-file=.env --name sm-sentinel -v sm-sentinel-persistent:/app/.storage sm-sentinel
```

## Container images

Public container images are published to GitHub Container Registry for this repository:

```bash
docker pull ghcr.io/lidofinance/sm-sentinel:latest
```

Release publishing is driven by pull request labels and a final GitHub release publication:

- Label merged pull requests with `release:major`, `release:minor`, or `release:patch`.
- Each merge to `main` prepares or refreshes a draft GitHub Release with a stable SemVer tag such as `v1.2.3`.
- If multiple pull requests merge before publication, the pending draft release is refreshed to point at the latest merge commit and the highest required SemVer bump.
- Review or edit the draft release notes, then use GitHub's **Publish release** button.
- Publishing the release builds and pushes `ghcr.io/lidofinance/sm-sentinel:1.2.3`, `1.2`, `1`, and `latest`.

The same `release:*` labels are used for both version bump selection and generated release note categories.
Draft releases do not publish container images; only the explicit GitHub release publication does.

Every image exposes its release version, Git branch, and commit as JSON at
`http://<pod>:8080/build-info.json`.

Kubernetes images are promoted through Harbor as follows:

- merges into `develop` publish the mutable `dev` tag for testnet;
- merges into `main` publish the mutable `staging` tag;
- publishing a GitHub release publishes its immutable `vX.Y.Z` tag to production.

Production releases remain manual: merge the promotion pull request from `develop`
to `main`, verify staging, then publish the prepared draft GitHub release.

## Local development

Install dependencies and run the bot with `uv`:

```bash
uv sync
uv run python -m sentinel.main
```

## Running alongside eth-docker
If you are running the bot on the same machine as the [eth-docker](https://github.com/eth-educators/eth-docker), 
you can use the execution client with no need to expose it outside the container.

You need to use a special docker-compose file that connects the Sentinel instance to the eth-docker network.

```bash
docker compose -f docker-compose-ethd.yml up -d
```

`WEB3_SOCKET_PROVIDERS` env variable is set to `ws://execution:8546` via docker-compose file,
so you don't need to specify it in the `.env` file.

## Extra configuration

Pass the `BLOCK_FROM` environment variable to specify the block the bot should start monitoring events from.
Note that this may result in duplicate events if you set it to a block that the bot has already processed before.
`BLOCK_FROM=0` allows you to skip processing past blocks and always start from the head.
In general, you don't need to set this variable.

`BLOCK_BATCH_SIZE` controls how many blocks are fetched per RPC request when processing historical events.
The default value is `10000`.

`PROCESS_BLOCKS_REQUESTS_PER_SECOND` caps how many historical RPC requests `process_blocks_from`
issues per second when backfilling. Leave unset to disable throttling.

## Admin access (ADMIN_IDS)

Some maintenance commands are restricted to admins. To enable them, set your Telegram user ID in the `ADMIN_IDS` environment variable.

How to find your Telegram user ID:
- Open a chat with `@userinfobot`, send `/start`, and copy the numeric `Id` value.

Configure the ID in your `.env`:

```
# Single admin
ADMIN_IDS=123456789

# Multiple admins (comma or space separated)
ADMIN_IDS=123456789,987654321
# or
ADMIN_IDS=123456789 987654321
```

### Admin broadcasts

Admins can broadcast messages via the in-bot Admin panel:

- Open `Admin` → `Broadcast`.
- Choose `All subscribers` to send a message to every chat subscribed to any node operator, then enter your message.
- Or choose `By node operator` and enter comma-separated node operator IDs (e.g., `1,2,3`), then enter your message.

## Integration test suite

End-to-end verification for on-chain events lives under `tests/integration`. Each
scenario replays a real transaction through a lightweight harness that exposes the
same `process_blocks_from` and `subscribe` entrypoints as the real subscription, allowing
the module event message engine to render the expected Markdown for every event.

To enable the suite:

1. Install a local fork provider:
   - [`anvil`](https://book.getfoundry.sh/anvil/)
2. Ensure `.env` contains a `WEB3_SOCKET_PROVIDERS` entry that can serve archive data;
   the tests reuse this value as the fork source (WebSocket URLs are
   translated to their HTTP equivalents automatically)

Each test spawns a dedicated local fork pinned to the case's block to keep state
deterministic. Run the suite with:

```
uv run pytest -m integration
```
