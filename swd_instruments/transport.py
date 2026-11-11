"""ST-Link/SWD 传输层：只读诊断 + 极窄字段写入。

真实写入仅通过 `write_masked()`，地址和掩码由 core.REAL_WRITE_MASKS 限定；
raw `write32()` 永远拒绝。Flash/RAM、ADC1_DR 和通用 ADC2_DR 读取不在白名单。
ADC2_DR 只允许在 hardware 层取得 ADC2 所有权后经 `read_adc2_dr()` 读取。
"""
from __future__ import annotations

from . import pins as _pins
from .core import (
    ADC2_DR, RCC_CFGR, FLASH_BASE, FLASH_SIZE_REG, REAL_WRITE_MASKS, SAFE_READ_ADDRESSES,
    SafetyError,
)

SESSION_OPTIONS = {
    'connect_mode': 'attach',
    'resume_on_disconnect': False,
    'auto_unlock': False,
    'no_config': True,
    'target_override': 'cortex_m',  # Generic core, no STM32 flash algorithm/pack.
    # 2026-11-10 实物分档测速后由 100 kHz 提高到 950 kHz；结果见仓库 README 的“SWD 时钟”一节。
    'frequency': 950_000,
    'cache.enable_memory': False,
    'cache.enable_register': False,
    'pack': [],
}

MAX_FLASH_BYTES = 64 * 1024

# SWD时钟只能取ST-Link V2固件支持的固定档位，不接受任意数值。
# 提高时钟不减少任何保护检查，只缩短每次SWD访问的线上时间；
# 哪一档可用取决于实际接线，必须接板验证后才能改默认值。
# 当前默认 950 kHz 比实物通过的最高档（1.8 MHz）低一档，留出余量；
# 接线不稳时可退回 100 kHz。4.6 MHz 未登记、未测试。
DEFAULT_SWD_HZ = SESSION_OPTIONS['frequency']
ALLOWED_SWD_HZ = (100_000, 240_000, 480_000, 950_000, 1_800_000)


class PyOCDSWD:
    """必须显式打开并指定完整 ST-Link 序列号。

    attach 仍会配置调试访问和调试硬件，不是零影响证明；禁止与其他调试器并用。
    """
    def __init__(self, serial_number: str, swd_hz: int = DEFAULT_SWD_HZ):
        if not serial_number.strip():
            raise ValueError('必须指定ST-Link完整序列号，不自动连接第一块板子')
        if type(swd_hz) is not int or swd_hz not in ALLOWED_SWD_HZ:
            raise ValueError(f'SWD时钟只能是已登记档位之一：{ALLOWED_SWD_HZ}')
        self.swd_hz = swd_hz
        self.serial_number = serial_number.strip()
        self.session = None
        self._adc2_data_owner = False
        self._flash_capacity = None
        self._write_lease_session = None
        self._adcpre_write_authorized = False
        self._selection_masks = None
        self._flash_evidence = {}
        self._observed_hash = None
        self.write_log: list[tuple[int, int, int]] = []

    def open(self):
        if self.session is not None:
            raise SafetyError('诊断会话已经打开')
        try:
            from pyocd.core.helpers import ConnectHelper
            from pyocd.core.session import Session
            from pyocd.probe.stlink_probe import StlinkProbe
        except ImportError as exc:
            raise RuntimeError('未安装兼容的可选pyocd；模拟模式仍可使用。请参见README。') from exc
        if not hasattr(Session, '_load_user_script'):
            raise SafetyError('pyocd用户脚本隔离接口不匹配，拒绝连接')

        class NoScriptSession(Session):
            def _load_user_script(self):
                # no_config仅禁止YAML，并不会禁止自动pyocd_user.py。
                return None

        probes = [probe for probe in ConnectHelper.get_all_connected_probes(blocking=False)
                  if isinstance(probe, StlinkProbe) and probe.unique_id == self.serial_number]
        if len(probes) != 1:
            raise SafetyError('未找到唯一匹配的ST-Link；未连接任何目标')
        options = dict(SESSION_OPTIONS)
        options['frequency'] = self.swd_hz
        session = NoScriptSession(probes[0], options=options, auto_open=False)
        for key in ('connect_mode', 'resume_on_disconnect', 'auto_unlock', 'no_config'):
            if session.options.get(key) != SESSION_OPTIONS[key]:
                raise SafetyError(f'pyocd安全选项回读不符：{key}')
        if session.options.get('frequency') != self.swd_hz:
            raise SafetyError('pyocd SWD时钟选项回读不符')
        self.session = session
        try:
            session.open()
            if not self.is_running():
                raise SafetyError('目标不是运行状态；不会主动恢复、暂停或复位')
        except Exception as original:
            try:
                self.close()
            except Exception as cleanup:
                raise SafetyError(f'连接失败：{original}；清理也失败：{cleanup}') from original
            raise
        return self

    def read32(self, address: int) -> int:
        if address not in SAFE_READ_ADDRESSES:
            if address in (0x08000000,):
                raise SafetyError('Flash必须通过read_flash_block只读区间读取')
            raise SafetyError(f'拒绝读取0x{address:08X}：非只读诊断白名单')
        if self.session is None:
            raise SafetyError('诊断尚未连接')
        if not self.is_running():
            raise SafetyError('检测到目标已停止，拒绝继续读取')
        return int(self.session.target.read32(address))

    def read_flash_block(self, address: int, size: int) -> bytes:
        """只读原固件；当前仅支持已审查的64KiB C8目标。"""
        if type(address) is not int or type(size) is not int:
            raise SafetyError('Flash读取参数必须是整数')
        if address < FLASH_BASE or size <= 0 or size > MAX_FLASH_BYTES:
            raise SafetyError('拒绝越界Flash读取')
        if address + size > FLASH_BASE + MAX_FLASH_BYTES:
            raise SafetyError('拒绝越界Flash读取')
        if self.session is None:
            raise SafetyError('诊断尚未连接')
        if not self.is_running():
            raise SafetyError('检测到目标已停止，拒绝继续读取')
        if self._flash_capacity is None:
            self.read_flash_size_kb()
        if address + size > FLASH_BASE + self._flash_capacity:
            raise SafetyError('Flash读取超出当前目标已确认容量')
        target = self.session.target
        try:
            data = target.read_memory_block8(address, size)
        except AttributeError as exc:
            raise SafetyError('pyocd缺少受限Flash块读取接口，拒绝扩大读取区间') from exc
        data = bytes(data)
        if len(data) != size:
            raise SafetyError('Flash块读取长度与请求不符')
        self._flash_evidence[address] = data
        import hashlib
        cursor = FLASH_BASE
        chunks = []
        while cursor in self._flash_evidence:
            block = self._flash_evidence[cursor]
            chunks.append(block)
            cursor += len(block)
        if cursor == FLASH_BASE + MAX_FLASH_BYTES:
            self._observed_hash = (id(self.session), hashlib.sha256(b"".join(chunks)).hexdigest())
        return data

    def read_flash_size_kb(self) -> int:
        capacity = self.read32(FLASH_SIZE_REG) & 0xFFFF
        if capacity != 64:
            raise SafetyError('当前已审查驱动仅支持64KiB的STM32F103C8')
        self._flash_capacity = capacity * 1024
        return capacity

    def write32(self, address: int, value: int):
        # 不提供任意地址写入；真实字段写入只能走 write_masked。
        raise SafetyError('拒绝raw write32：真实写入仅允许受限字段事务')

    def write_masked(self, address: int, mask: int, value: int) -> tuple[int, int]:
        """受限读改写，保留掩码外所有位；返回(改写前, 读回值)。"""
        if address not in REAL_WRITE_MASKS or type(mask) is not int or mask <= 0:
            raise SafetyError('地址或掩码不在真实写入白名单')
        if mask & ~REAL_WRITE_MASKS[address] or value & ~mask:
            raise SafetyError('写入字段越界')
        if self.session is None:
            raise SafetyError('真实会话尚未连接')
        if self._write_lease_session != id(self.session):
            raise SafetyError('当前会话尚未通过已审查原固件验证，禁止外设写入')
        if address == RCC_CFGR and not self._adcpre_write_authorized:
            raise SafetyError('当前profile未授权修改共享ADCPRE')
        if not self.is_running():
            raise SafetyError('目标已停止，拒绝写入')
        if self._selection_masks is None or mask & ~self._selection_masks.get(address, 0):
            raise SafetyError('字段不属于当前激活选择')
        target = self.session.target
        from .core import COMMAND_REGISTERS, TIM3_SR, TIM4_SR
        if address in COMMAND_REGISTERS:  # 命令寄存器不可读取或RMW。
            target.write32(address, value & mask)
            self.write_log.append((address, mask, value & mask))
            return 0, 0
        before = int(target.read32(address))
        # rc_w0: 掩码外写1，避免清除读取后新到达的标志。
        after = ((0xFFFFFFFF & ~mask) | value) if address in tuple(_pins.timer_registers(t).SR for t in _pins.TIMER_BASES) else ((before & ~mask) | (value & mask))
        target.write32(address, after)
        self.write_log.append((address, mask, value & mask))
        return before, int(target.read32(address))

    def authorize_profile(self, profile, observed_firmware_sha256: str):
        self._selection_masks = None
        from .profiles import assert_trusted_profile
        assert_trusted_profile(profile)
        if (self.session is None or not self.is_running()
                or observed_firmware_sha256 != profile.firmware.image_sha256):
            raise SafetyError('没有当前运行目标的完整镜像匹配证据')
        self._write_lease_session = id(self.session)
        self._adcpre_write_authorized = profile.resources['adcpre_free']

    def activate_selection(self, masks):
        if self._write_lease_session != id(self.session) or self.session is None:
            raise SafetyError('选择授权需要当前会话写租约')
        checked = dict(masks)
        for address, mask in checked.items():
            if type(mask) is not int or mask <= 0 or mask & ~REAL_WRITE_MASKS.get(address, 0):
                raise SafetyError('选择字段超出候选白名单')
            if address == RCC_CFGR and not self._adcpre_write_authorized:
                raise SafetyError('未授权ADCPRE')
        self._selection_masks = checked

    def authorize_general_mode(self, observed_firmware_sha256, confirmed=False):
        self._selection_masks = None
        if (confirmed is not True or self.session is None or not self.is_running()
                or self._observed_hash != (id(self.session), observed_firmware_sha256)):
            raise SafetyError('通用模式需要本会话完整镜像摘要、运行状态及明确风险确认')
        self._write_lease_session = id(self.session)
        self._adcpre_write_authorized = False

    def revoke_resource_access(self):
        self._selection_masks = None
        self._write_lease_session = None
        self._adcpre_write_authorized = False
        self._adc2_data_owner = False

    def claim_adc2_data_owner(self):
        if (self.session is None or not self.is_running()
                or self._write_lease_session != id(self.session)):
            raise SafetyError('目标未运行，不能取得ADC2数据所有权')
        self._adc2_data_owner = True

    def release_adc2_data_owner(self):
        self._adc2_data_owner = False

    def read_adc2_dr(self) -> int:
        if not self._adc2_data_owner:
            raise SafetyError('尚未取得ADC2所有权，禁止读取ADC2_DR')
        if self.session is None or not self.is_running():
            raise SafetyError('目标未运行，拒绝读取ADC2_DR')
        return int(self.session.target.read32(ADC2_DR))

    def is_running(self) -> bool:
        if self.session is None:
            return False
        return bool(self.session.target.is_running())

    def close(self):
        self.revoke_resource_access()
        self._flash_capacity = None
        self._flash_evidence.clear()
        self._observed_hash = None
        if self.session is not None:
            session, self.session = self.session, None
            session.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()


# 兼容旧导入名；行为已扩展为受控字段写入。
PyOCDDiagnostic = PyOCDSWD
