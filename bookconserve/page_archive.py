"""页级封存与完整状态裁定。

完整性只由 :mod:`bookconserve.tile_capture` 给出的物理区域并集与来源
类别决定，不再由上传数量决定。三次上传若只落在两个物理区域，状态
仍是缺图；代理转码件即使字节不同也不改变覆盖。本模块只产出覆盖
地图与缺图清单，不判纸材来源，也不判损伤责任。

馆方依法可走的两条补录路径（版面范围、来源等级与待补拍区域不同）：

- 路径 A：找回扫描仪签发的底部原始瓦片及坐标校准板 → 可签整页均由
  原始扫描覆盖；
- 路径 B：原始瓦片损坏，仅有独立相机补拍左下 → 只给左下辅助来源
  证书，右下仍缺图，辅助照片不得伪称扫描仪原件。

旧的错误报告与 tile-c 转码路径始终保留，可随时复核。
"""
from __future__ import annotations

from . import tile_capture as tc

#: 旧规则下仅凭上传数给出的状态（保留用于复现错误报告）。
LEGACY_COMPLETE = '完整'
LEGACY_MISSING = '缺图'
#: 新裁定状态。
STATUS_COMPLETE = '完整'
STATUS_MISSING = '缺图'
STATUS_AUX_ONLY = '辅助覆盖待原始补扫'
STATUS_CALIBRATION = '原始覆盖齐备待校准签封'


# ---------------------------------------------------------------------------
# 旧规则（错误报告复现，不得用于当前裁定）
# ---------------------------------------------------------------------------

def legacy_assess(rows, expected: int = 3) -> dict:
    """复现旧错误规则：只数 upload_id，达到 expected 即判完整。"""
    bundle = tc.parse_bundle(rows)
    count = tc.uploads(bundle)
    return {'uploads': count, 'status': LEGACY_COMPLETE if count >= expected else LEGACY_MISSING}


# ---------------------------------------------------------------------------
# 当前裁定
# ---------------------------------------------------------------------------

def assess(rows, expected: int = 3) -> dict:
    """按物理区域并集与来源等级裁定纸页封存状态。

    返回字典保留 ``uploads`` 与 ``status`` 两个旧键，另附覆盖地图、
    缺图清单、遮挡/折边、转码链与封存问题，供逐条复核。``expected``
    仅在旧规则复现中使用，新规则不接受以数量代替区域并集。
    """
    bundle = tc.validate(tc.parse_bundle(rows))
    page = bundle['page']
    if not (page.get('page_width_mm') and page.get('page_height_mm')):
        # 没有坐标网格的旧数据无法配准，不能判完整。
        return {
            'uploads': tc.uploads(bundle),
            'status': STATUS_MISSING,
            'reason': '缺少扫描仪原始坐标网格，无法按物理区域配准',
            'legacy_report': legacy_assess(bundle, expected),
        }

    cmap = tc.coverage_map(bundle)
    missing = tc.missing_list(bundle)
    unscanned = [m['grid_cell'] for m in missing if m['reason'] == 'missing']
    raw_missing = [m['grid_cell'] for m in missing if m['reason'] == 'raw_missing']
    seal_problems = tc.seal_manifest_problems(bundle)
    board = page.get('calibration_board') or {}
    board_ok = bool(board.get('present')) and bool(board.get('grid_signature'))

    if raw_missing:
        status = STATUS_AUX_ONLY
        reason = '部分区域仅辅助来源覆盖，缺扫描仪原始瓦片'
    elif unscanned:
        status = STATUS_MISSING
        reason = '存在未扫描物理区域，转码副本不构成覆盖'
    elif not board_ok or seal_problems:
        status = STATUS_CALIBRATION
        reason = '原始覆盖齐备，但坐标校准板或页级封存清单未完成'
    else:
        status = STATUS_COMPLETE
        reason = '可验证原始扫描区域覆盖整页，校准板与封存清单齐备'

    return {
        'page_id': page.get('page_id'),
        'physical_sheet': page.get('physical_sheet'),
        'page_turn': page.get('page_turn'),
        'uploads': tc.uploads(bundle),
        'status': status,
        'reason': reason,
        'coverage_map': cmap,
        'missing_list': missing,
        'unscanned_cells': unscanned,
        'raw_missing_cells': raw_missing,
        'occluded_regions': tc.occluded_regions(bundle),
        'folded_edges': tc.folded_edges(bundle),
        'transcode_chain': tc.transcode_chain(bundle['tiles']),
        'calibration_board': board,
        'seal_manifest': bundle.get('seal_manifest'),
        'seal_problems': seal_problems,
        'legacy_report': {'uploads': tc.uploads(bundle),
                          'status': legacy_assess(bundle, expected)['status']},
    }


# ---------------------------------------------------------------------------
# 两种合法补录路径
# ---------------------------------------------------------------------------

def _whole_cell_tile(upload_id: str, tier: str, cell_name: str, bundle: dict,
                     sha: str, device: str, pixel_width: int = 600,
                     pixel_height: int = 800, **extra) -> dict:
    page = bundle['page']
    cell = tc.grid_cells(page)[cell_name]
    a = (cell['x1'] - cell['x0']) / pixel_width
    b = (cell['y1'] - cell['y0']) / pixel_height
    tile = {
        'upload_id': upload_id,
        'page_id': page['page_id'],
        'device': device,
        'tier': tier,
        'grid_cell': cell_name,
        'region': cell_name,
        'sha': sha,
        'pixel_width': pixel_width,
        'pixel_height': pixel_height,
        'transform': [a, b, cell['x0'], cell['y0']],
        'footprint': dict(cell),
    }
    tile.update(extra)
    return tile


def _append_seal_entry(bundle: dict, tile: dict) -> dict:
    manifest = dict(bundle.get('seal_manifest') or {})
    entries = [dict(e) for e in manifest.get('entries', ())]
    if not any(e.get('upload_id') == tile['upload_id'] for e in entries):
        entries.append({'upload_id': tile['upload_id'], 'sha': tile['sha'], 'tier': tile['tier']})
    manifest['entries'] = entries
    return {**bundle, 'seal_manifest': manifest}


def supplement_raw_tile(bundle: dict, upload_id: str, sha: str, cells,
                        board_signature: str) -> dict:
    """路径 A：补录扫描仪签发的底部原始瓦片与坐标校准板。

    整页均可由原始扫描覆盖时才允许调用；返回修订后的同一物理页
    采录包，旧瓦片与转码链原样保留。
    """
    bundle = tc.parse_bundle(bundle)
    cells = [cells] if isinstance(cells, str) else list(cells)
    page = dict(bundle['page'])
    missing_cells = {m['grid_cell'] for m in tc.missing_list(bundle)}
    for cell_name in cells:
        if cell_name not in missing_cells:
            raise tc.TileCaptureError(f'{cell_name} 并非缺图单元，不能重复补录原始瓦片')
        tile = _whole_cell_tile(upload_id if len(cells) == 1 else f'{upload_id}-{cell_name}',
                                tc.TIER_RAW, cell_name, bundle,
                                sha if len(cells) == 1 else f'{sha}-{cell_name}',
                                device='scanner')
        bundle = tc.register_late_tile(bundle, tile)
        bundle = _append_seal_entry(bundle, tile)
    page['calibration_board'] = {
        'present': True,
        'grid_signature': board_signature,
        'note': '扫描仪签发坐标校准板，随底部原始瓦片补录',
    }
    bundle = {**bundle, 'page': page}
    return tc.validate(bundle)


def supplement_auxiliary_tile(bundle: dict, upload_id: str, sha: str, cell_name: str) -> dict:
    """路径 B：原始瓦片损坏，以独立相机补拍一个区域。

    辅助照片等级固定为 auxiliary，不进入扫描仪封存清单，也不能把
    其余未扫描区域抹成完整。
    """
    bundle = tc.parse_bundle(bundle)
    missing_cells = {m['grid_cell'] for m in tc.missing_list(bundle)}
    if cell_name not in missing_cells:
        raise tc.TileCaptureError(f'{cell_name} 并非缺图单元，无需辅助补拍')
    tile = _whole_cell_tile(upload_id, tc.TIER_AUX, cell_name, bundle, sha,
                            device='independent-camera')
    return tc.register_late_tile(bundle, tile)


# ---------------------------------------------------------------------------
# 来源证书
# ---------------------------------------------------------------------------

def raw_page_certificate(bundle: dict) -> dict:
    """路径 A 证书：整页均由扫描仪原始扫描覆盖。"""
    bundle = tc.validate(tc.parse_bundle(bundle))
    result = assess(bundle)
    legal = result['status'] == STATUS_COMPLETE
    return {
        'certificate': '整页原始扫描覆盖证书',
        'page_id': bundle['page'].get('page_id'),
        'legal': legal,
        'source_tier': tc.TIER_RAW,
        'scope': list(tc.GRID_CELLS),
        'raw_cells': result['coverage_map']['raw_cells'],
        'calibration_board': result['calibration_board'],
        'pending_cells': result['unscanned_cells'] + result['raw_missing_cells'],
        'reason': result['reason'],
    }


def auxiliary_certificate(bundle: dict, cell_name: str) -> dict:
    """路径 B 证书：单个区域的独立相机辅助来源证书。

    证书只覆盖声明单元；其余缺图单元继续保留在待补拍清单中。
    """
    bundle = tc.validate(tc.parse_bundle(bundle))
    result = assess(bundle)
    aux_cells = result['coverage_map']['auxiliary_cells']
    legal = cell_name in aux_cells and cell_name not in result['coverage_map']['raw_cells']
    return {
        'certificate': '辅助来源补拍证书',
        'page_id': bundle['page'].get('page_id'),
        'legal': legal,
        'source_tier': tc.TIER_AUX,
        'scope': [cell_name] if legal else [],
        'auxiliary_cells': aux_cells,
        'pending_cells': result['unscanned_cells'] + result['raw_missing_cells'],
        'note': '辅助照片不得伪称扫描仪原件；未覆盖区域仍须补拍',
        'reason': result['reason'],
    }
