"""Config flow for Uconnect integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError

from py_uconnect.api import API
from py_uconnect.brands import BRANDS as BRANDS_BY_NAME

from .const import (
    BRANDS,
    CONF_BRAND_REGION,
    CONF_DISABLE_TLS_VERIFICATION,
    CONF_ADD_COMMAND_ENTITIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_PIN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_VIN = "vin"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_BRAND_REGION): vol.In(BRANDS),
        vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
        vol.Required(CONF_DISABLE_TLS_VERIFICATION, default=False): bool,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_SCAN_INTERVAL,
            default=DEFAULT_SCAN_INTERVAL,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=999)),
        vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
        vol.Required(CONF_ADD_COMMAND_ENTITIES): bool,
    }
)


async def validate_input(hass: HomeAssistant, user_input: dict[str, Any]):
    """Validate the user input allows us to connect. Returns vehicle dict or None if fetch failed."""

    api = API(
        email=user_input[CONF_USERNAME],
        password=user_input[CONF_PASSWORD],
        pin=user_input[CONF_PIN],
        brand=BRANDS_BY_NAME[BRANDS[user_input[CONF_BRAND_REGION]]],
        disable_tls_verification=user_input[CONF_DISABLE_TLS_VERIFICATION],
    )

    try:
        await hass.async_add_executor_job(api.login)
    except Exception as e:
        _LOGGER.exception(f"Authentication failed: {e}")
        raise InvalidAuth

    try:
        await hass.async_add_executor_job(api.refresh)
        return api.get_vehicles()
    except Exception as e:
        _LOGGER.warning(f"Could not fetch vehicle list automatically: {e}")
        return None


class UconnectOptionFlowHandler(config_entries.OptionsFlow):
    """Handle an option flow for Uconnect"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options init setup."""

        if user_input is not None:
            return self.async_create_entry(
                title=self.config_entry.title, data=user_input
            )

        return self.async_show_form(
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Uconnect"""

    VERSION = 1
    reauth_entry: ConfigEntry | None = None

    def __init__(self):
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._vehicles: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Initiate options flow instance."""
        return UconnectOptionFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""

        if user_input is None:
            return self.async_show_form(data_schema=STEP_USER_DATA_SCHEMA)

        errors = {}

        try:
            vehicles = await validate_input(self.hass, user_input)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            self._user_input = user_input
            self._vehicles = vehicles or {}

            if vehicles is None:
                return await self.async_step_manual_vin()

            if len(self._vehicles) > 1:
                return await self.async_step_vehicle()

            return self._create_entry(user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    def _create_entry(self, data: dict) -> ConfigFlowResult:
        """Create the config entry or update reauth entry."""
        if self.reauth_entry is None:
            title = f"{BRANDS[data[CONF_BRAND_REGION]]} {data[CONF_USERNAME]}"
            if CONF_VIN in data:
                title += f" ({data[CONF_VIN]})"
            unique_id = hashlib.sha256(title.encode("utf-8")).hexdigest()
            self.hass.async_create_task(self.async_set_unique_id(unique_id))
            return self.async_create_entry(title=title, data=data)
        else:
            self.hass.config_entries.async_update_entry(
                self.reauth_entry, data=data
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
            )
            return self.async_abort(reason="reauth_successful")

    async def async_step_manual_vin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fallback step: let user enter VIN manually when auto-fetch fails."""

        if user_input is not None:
            data = {**self._user_input, CONF_VIN: user_input[CONF_VIN].strip().upper()}
            return self._create_entry(data)

        manual_vin_schema = vol.Schema(
            {vol.Required(CONF_VIN): str}
        )

        return self.async_show_form(
            step_id="manual_vin",
            data_schema=manual_vin_schema,
        )

    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which vehicle to configure."""

        if user_input is not None:
            selected_vin = user_input[CONF_VIN]
            data = {**self._user_input, CONF_VIN: selected_vin}
            return self._create_entry(data)

        vin_options = {}
        for vin, vehicle in self._vehicles.items():
            label = f"{vehicle.year} {vehicle.make} {vehicle.model} ({vin})"
            vin_options[vin] = label

        vehicle_schema = vol.Schema(
            {vol.Required(CONF_VIN): vol.In(vin_options)}
        )

        return self.async_show_form(
            step_id="vehicle",
            data_schema=vehicle_schema,
        )

    async def async_step_reauth(self, user_input=None):
        """Perform reauth upon an API authentication error."""
        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )
        self._reauth_config = True
        return await self.async_step_user()


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
