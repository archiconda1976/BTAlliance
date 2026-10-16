"""Alliance BT 1.0.40 device-type labels (fb.d and English resources)."""

TYPE_NAMES = {
    0x00: 'Strips', 0x01: 'Chandelier', 0x02: 'TrackLight', 0x03: 'Walkway',
    0x04: 'PanelLight', 0x05: 'TableLamp', 0x06: 'CeilingLight', 0x07: 'Planter',
    0x08: 'BL200-bt', 0x09: 'B_ShapeCandleLight', 0x0A: 'BR_Light',
    0x0B: 'G_ShapeBulb', 0x0C: 'DL-bt', 0x0D: 'MR16_Spotlight', 0x0E: 'PARLight',
    0x10: 'Scene Light', 0x11: 'Gimbal Light', 0x12: 'Special shaped Lamp',
    0x13: 'Relay', 0x20: 'T5T8_Tubes', 0x21: 'T5T8_Integrated_Tubes',
    0x22: 'DownLight', 0x25: 'GL-bt',
    0xA0: 'SL50-bt', 0xA1: 'UL100-bt', 0xA2: 'SL25-bt', 0xA3: 'SL35-bt',
    0xA4: 'ASLTEM-bt', 0xA5: 'FL50-bt', 0xA7: 'Switch', 0xA8: 'BL400-bt',
    0xA9: 'FL200-bt', 0xAA: 'HL100-bt', 0xAB: 'HL200-bt', 0xAC: 'PL300-bt',
    0xAD: 'WL400-bt', 0xAE: 'GL3-bt', 0xB1: 'PL100-bt', 0xB2: 'SL70-bt',
    0xB3: 'WS350-bt', 0xB4: 'BL900-bt', 0xB5: 'PLUGIN-bt',
    0xB6: 'BTRE_SM-bt', 0xB7: 'BTRE-bt',
}


def device_type_name(device_type: int | None) -> str | None:
    """Translate recognized codes; unknown values remain unlabeled."""
    if not isinstance(device_type, int) or isinstance(device_type, bool) or not 0 <= device_type <= 0xFFFF:
        return None
    family = device_type >> 8
    if 0xA0 <= family <= 0xA6:
        return TYPE_NAMES.get(device_type & 0xFF)
    if device_type == 0xC000:
        return 'HandControl'
    if device_type == 0xC001:
        return 'PanelSwitch'
    if device_type == 0xC100:
        return 'Gateway'
    if family == 0xC3:
        return 'Switch'
    if family == 0xC4:
        return 'Socket'
    return None
