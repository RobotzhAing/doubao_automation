"""Video model names and daily limits shared by the workbench and runner."""

MINI = 'mini'
FAST = 'fast'
MODEL_LABELS = {MINI: 'Seedance 2.0 Mini', FAST: 'Seedance 2.0 Fast'}
MODEL_LIMITS = {MINI: 4, FAST: 2}


def normalize_model(value):
    if value is None:
        return MINI
    if isinstance(value, str):
        selected = value.strip().lower()
        if selected in MODEL_LABELS:
            return selected
    raise ValueError('请选择 Seedance 2.0 Mini 或 Seedance 2.0 Fast')
