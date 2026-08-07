import os
from dataclasses import dataclass
from datetime import time, timezone

from sentinel.app.contracts import ContractAddresses


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for token in raw.replace(" ", ",").split(","):
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            # Ignore invalid entries silently at config load time
            continue
    return ids


@dataclass(frozen=True)
class ConfigValues:
    # Paths and tokens
    filestorage_path: str
    token: str | None
    web3_socket_providers: tuple[str, ...]
    healthcheck_host: str
    healthcheck_port: int

    # URLs
    etherscan_url: str | None
    beaconchain_url: str | None
    module_ui_url: str | None

    # Other
    block_batch_size: int
    process_blocks_requests_per_second: float | None
    block_from: int | None
    admin_ids: set[int]
    deposit_digest_time: time

    # Derived URL templates
    @property
    def etherscan_block_url_template(self) -> str | None:
        return None if not self.etherscan_url else f"{self.etherscan_url}/block/{{}}"

    @property
    def etherscan_tx_url_template(self) -> str | None:
        return None if not self.etherscan_url else f"{self.etherscan_url}/tx/{{}}"

    @property
    def beaconchain_url_template(self) -> str | None:
        return None if not self.beaconchain_url else f"{self.beaconchain_url}/validator/{{}}"


@dataclass(frozen=True)
class EnvConfig(ConfigValues):
    module_address: str

    def resolve(self, contract_addresses: ContractAddresses) -> "Config":
        return Config(
            filestorage_path=self.filestorage_path,
            token=self.token,
            web3_socket_providers=self.web3_socket_providers,
            healthcheck_host=self.healthcheck_host,
            healthcheck_port=self.healthcheck_port,
            contract_addresses=contract_addresses,
            etherscan_url=self.etherscan_url,
            beaconchain_url=self.beaconchain_url,
            module_ui_url=self.module_ui_url,
            block_batch_size=self.block_batch_size,
            process_blocks_requests_per_second=self.process_blocks_requests_per_second,
            block_from=self.block_from,
            admin_ids=self.admin_ids,
            deposit_digest_time=self.deposit_digest_time,
        )


@dataclass(frozen=True)
class Config(ConfigValues):
    contract_addresses: ContractAddresses


_CONFIG: Config | None = None


def _parse_healthcheck_port(raw: str | None) -> int:
    if not raw:
        return 8080
    port = int(raw)
    if port <= 0 or port > 65535:
        raise RuntimeError("HEALTHCHECK_PORT must be between 1 and 65535")
    return port


def _parse_deposit_digest_time(raw: str | None) -> time:
    value = raw or "09:00"
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("DEPOSIT_DIGEST_TIME must use HH:MM format") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise RuntimeError("DEPOSIT_DIGEST_TIME must use HH:MM format")
    return parsed.replace(tzinfo=timezone.utc)


def get_healthcheck_bind_from_env() -> tuple[str, int]:
    return (
        os.getenv("HEALTHCHECK_HOST", "0.0.0.0"),
        _parse_healthcheck_port(os.getenv("HEALTHCHECK_PORT")),
    )


def _parse_provider_urls(raw: str) -> tuple[str, ...]:
    providers: list[str] = []
    for token in raw.split(","):
        provider = token.strip()
        if provider and provider not in providers:
            providers.append(provider)
    return tuple(providers)


def load_config_from_env() -> EnvConfig:
    filestorage_path = os.getenv("FILESTORAGE_PATH", ".storage")
    token = os.getenv("TOKEN")
    raw_web3_socket_providers = os.getenv("WEB3_SOCKET_PROVIDERS") or os.getenv(
        "WEB3_SOCKET_PROVIDER"
    )
    healthcheck_host, healthcheck_port = get_healthcheck_bind_from_env()
    module_address = os.getenv("MODULE_ADDRESS")

    if not raw_web3_socket_providers:
        raise RuntimeError(
            "WEB3_SOCKET_PROVIDERS or legacy WEB3_SOCKET_PROVIDER must be configured"
        )
    if not module_address:
        raise RuntimeError("MODULE_ADDRESS must be configured")

    provider_urls = _parse_provider_urls(raw_web3_socket_providers)
    if not provider_urls:
        raise RuntimeError("WEB3_SOCKET_PROVIDERS must contain at least one provider URL")
    process_blocks_requests_per_second = os.getenv("PROCESS_BLOCKS_REQUESTS_PER_SECOND")
    if process_blocks_requests_per_second:
        process_blocks_requests_per_second = float(process_blocks_requests_per_second)
        if process_blocks_requests_per_second <= 0:
            raise RuntimeError("PROCESS_BLOCKS_REQUESTS_PER_SECOND must be positive")
    else:
        process_blocks_requests_per_second = None

    raw_block_from = os.getenv("BLOCK_FROM")
    block_from = int(raw_block_from) if raw_block_from else None

    return EnvConfig(
        filestorage_path=filestorage_path,
        token=token,
        web3_socket_providers=provider_urls,
        healthcheck_host=healthcheck_host,
        healthcheck_port=healthcheck_port,
        module_address=module_address,
        etherscan_url=os.getenv("ETHERSCAN_URL"),
        beaconchain_url=os.getenv("BEACONCHAIN_URL"),
        module_ui_url=os.getenv("MODULE_UI_URL"),
        block_batch_size=int(os.getenv("BLOCK_BATCH_SIZE", 10_000)),
        process_blocks_requests_per_second=process_blocks_requests_per_second,
        block_from=block_from,
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        deposit_digest_time=_parse_deposit_digest_time(os.getenv("DEPOSIT_DIGEST_TIME")),
    )


def get_config() -> Config:
    if _CONFIG is None:
        raise RuntimeError("Runtime config has not been resolved")
    return _CONFIG


def set_config(config: Config) -> None:
    global _CONFIG
    _CONFIG = config


def clear_config() -> None:
    """Basically for tests."""
    global _CONFIG
    _CONFIG = None
