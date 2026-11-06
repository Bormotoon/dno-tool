"""Wizard persistence delegates to the canonical configuration service."""


def save_all(ctx, pages, device_service, command_service, datastore, config, event_bus):
    from src.di import get_container
    from src.services.configuration.config_service import ConfigService

    return get_container().resolve(ConfigService).save_setup_config(ctx, datastore)
