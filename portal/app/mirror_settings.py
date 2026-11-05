"""The cache limit of the Hugging Face mirror (setting `hf_mirror_max_gib`): the one setting the mirror reads only when
it starts.

The portal keeps the value for the mirror in the ConfigMap `hf-mirror-settings` (key `max_gib`); the mirror pod takes
it as `HF_MIRROR_MAX_GIB_OVERRIDE` (optional, main/manifests/common-40-hf-mirror.yaml.tmpl) and it wins over the
`HF_MIRROR_MAX_GIB` of the .env. A setting without an override (never changed, or reset) is written as an EMPTY value:
the mirror then falls back to the .env, so «reset to .env» really means the .env and no old number stays pinned.
A restart of the pod applies it. The override the running pod uses is remembered in the flag `hf_mirror_applied_gib`
(no flag: the pod runs without an override, i.e. with the .env value), so the panel can say «применится после
перезапуска зеркала» while that differs from the saved value.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortalFlag, SystemSetting

KEY = "hf_mirror_max_gib"
CONFIGMAP = "hf-mirror-settings"
CONFIGMAP_KEY = "max_gib"
DEPLOYMENT = "hf-mirror"
APPLIED_FLAG = "hf_mirror_applied_gib"


async def override_of(db: AsyncSession) -> int | None:
    """The limit saved in the panel, None when the setting is not overridden (the mirror takes the .env)."""
    row = await db.get(SystemSetting, KEY)
    if row is None:
        return None
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return None


async def write_config(k8s, override: int | None) -> None:
    """The override the mirror takes at its next start; empty (no override) when None."""
    await k8s.put_config_map(CONFIGMAP, {CONFIGMAP_KEY: "" if override is None else str(int(override))})


async def applied(db: AsyncSession, env_default: int) -> int:
    """The limit the running mirror uses: what the last restart from the panel applied, else the .env value (the
    mirror has been running with it since the installation)."""
    flag = await db.get(PortalFlag, APPLIED_FLAG)
    try:
        return int(flag.value) if flag is not None else int(env_default)
    except ValueError:
        return int(env_default)


async def remember_applied(db: AsyncSession, override: int | None) -> None:
    """The restart applied `override` (None: the pod now runs on the .env value, the flag goes away)."""
    flag = await db.get(PortalFlag, APPLIED_FLAG)
    if override is None:
        if flag is not None:
            await db.delete(flag)
        return
    value = override
    if flag is None:
        db.add(PortalFlag(key=APPLIED_FLAG, value=str(int(value))))
    else:
        flag.value = str(int(value))
