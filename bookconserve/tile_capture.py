"""页图瓦片物理区域采录与覆盖配准。

修复拆页前必须留存覆盖整张纸页的原始影像。仅按 upload_id 计数或按
文件名推断区域都无法证明纸页各物理区域已扫描：同一影像经代理设备
重新编码后字节、压缩比与像素排列均可能不同，但它不构成任何新的
物理覆盖。本模块要求每条瓦片以同一张纸页的扫描仪原始坐标网格
（毫米坐标系）登记 footprint，再按几何并集计算覆盖，并区分

- ``raw``      扫描仪签发的原始瓦片（最高来源等级）；
- ``auxiliary`` 独立相机补拍，仅能授予辅助来源等级；
- ``transcode`` 代理设备对另一瓦片的转码副本，永远不增加覆盖。

主产物是可复核的页图覆盖地图与缺图清单；本模块不判纸材来源，
也不判损伤责任。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 来源等级：仅前两类构成可验证覆盖，转码不增加任何物理区域。
SOURCE_TIERS = ('raw', 'auxiliary', 'transcode')
#: 扫描仪原始瓦片的来源等级。
TIER_RAW = 'raw'
#: 独立相机补拍的辅助来源等级。
TIER_AUX = 'auxiliary'
#: 代理设备转码副本的来源等级：与字节是否相同无关，均不计覆盖。
TIER_TRANSCODE = 'transcode'

#: 翻页方向（古籍版面方向）。
PAGE_TURNS = ('right-to-left', 'left-to-right', 'top-to-bottom', 'unknown')

#: 2×2 扫描仪原始坐标网格单元，顺序固定、按坐标几何定位。
GRID_CELLS = ('top-left', 'top-right', 'bottom-left', 'bottom-right')
#: 允许的区域标签；网格单元名是规范标签，其余为自由描述。
KNOWN_REGIONS = frozenset(GRID_CELLS)

#: 浮点配准容差（毫米）。
EPS = 1e-9


class TileCaptureError(ValueError):
    """瓦片与同一纸页坐标系无法配准时抛出。"""


# ---------------------------------------------------------------------------
# 基础几何与坐标转换
# ---------------------------------------------------------------------------

def rect(x0: float, y0: float, x1: float, y1: float) -> dict:
    """构造规范化矩形（毫米，页坐标系，原点在左上角，x 向右、y 向下）。"""
    if x1 <= x0 or y1 <= y0:
        raise TileCaptureError(f'非法矩形: {(x0, y0, x1, y1)}')
    return {'x0': float(x0), 'y0': float(y0), 'x1': float(x1), 'y1': float(y1)}


def footprint_from_transform(tile: dict) -> dict:
    """按设备像素 -> 页面毫米的仿射变换解出瓦片 footprint。

    transform 为 ``[a, b, tx, ty]``，映射
    ``page_x = a * pixel_x + tx``、``page_y = b * pixel_y + ty``。
    瓦片覆盖设备像素矩形 ``(0,0)-(width,height)``，变换后必须为
    正向矩形，且与声明的 footprint（若给出）一致。
    """
    try:
        a, b, tx, ty = (float(v) for v in tile['transform'])
        width = float(tile['pixel_width'])
        height = float(tile['pixel_height'])
    except (KeyError, TypeError, ValueError) as exc:
        raise TileCaptureError(f"瓦片 {tile.get('upload_id')!r} 缺少可解码的像素坐标变换") from exc
    if width <= 0 or height <= 0 or a <= 0 or b <= 0:
        raise TileCaptureError(f"瓦片 {tile.get('upload_id')!r} 的像素尺寸或变换非正向")
    derived = rect(tx, ty, tx + a * width, ty + b * height)
    declared = tile.get('footprint')
    if declared is not None:
        declared = {k: float(declared[k]) for k in ('x0', 'y0', 'x1', 'y1')}
        if any(abs(derived[k] - declared[k]) > EPS for k in derived):
            raise TileCaptureError(
                f"瓦片 {tile.get('upload_id')!r} 声明 footprint 与坐标转换结果不一致"
            )
    return derived


def union(rects: Iterable[dict]) -> dict | None:
    """若干矩形的外接并集（不是逐格计数）。"""
    rects = list(rects)
    if not rects:
        return None
    return rect(
        min(r['x0'] for r in rects),
        min(r['y0'] for r in rects),
        max(r['x1'] for r in rects),
        max(r['y1'] for r in rects),
    )


def contains(outer: dict, inner: dict) -> bool:
    """outer 是否完整包含 inner（带容差）。"""
    return (
        outer['x0'] <= inner['x0'] + EPS
        and outer['y0'] <= inner['y0'] + EPS
        and outer['x1'] + EPS >= inner['x1']
        and outer['y1'] + EPS >= inner['y1']
    )


def intersects(a: dict, b: dict) -> bool:
    return not (a['x1'] <= b['x0'] + EPS or b['x1'] <= a['x0'] + EPS
                or a['y1'] <= b['y0'] + EPS or b['y1'] <= a['y0'] + EPS)


def grid_cells(page: dict) -> dict:
    """按页面宽高生成 2×2 扫描仪原始坐标网格（毫米）。"""
    width = float(page['page_width_mm'])
    height = float(page['page_height_mm'])
    if width <= 0 or height <= 0:
        raise TileCaptureError('页面物理尺寸非法')
    mid_x, mid_y = width / 2.0, height / 2.0
    return {
        'top-left': rect(0.0, 0.0, mid_x, mid_y),
        'top-right': rect(mid_x, 0.0, width, mid_y),
        'bottom-left': rect(0.0, mid_y, mid_x, height),
        'bottom-right': rect(mid_x, mid_y, width, height),
    }


def cell_of_footprint(page: dict, fp: dict) -> str | None:
    """footprint 完整落在哪个网格单元内；跨单元或越界返回 None。"""
    for name, cell in grid_cells(page).items():
        if contains(cell, fp):
            return name
    return None


# ---------------------------------------------------------------------------
# 来源分级与转码链
# ---------------------------------------------------------------------------

def source_tier(tile: dict) -> str:
    """来源等级：scanner/raw=raw，camera=auxiliary，proxy=transcode。

    兼容旧夹具：旧格式 ``device`` 字段直接映射；缺少 tier 时按设备推断。
    """
    tier = tile.get('tier')
    if tier is None:
        device = tile.get('device')
        tier = {'scanner': TIER_RAW, 'camera': TIER_AUX, 'proxy': TIER_TRANSCODE}.get(device)
    if tier not in SOURCE_TIERS:
        raise TileCaptureError(f"瓦片 {tile.get('upload_id')!r} 来源等级未知: {tier!r}")
    return tier


def is_transcode(tile: dict) -> bool:
    return source_tier(tile) == TIER_TRANSCODE


def transcode_chain(tiles: Iterable[dict]) -> list[dict]:
    """列出代理转码链；每条含转码件、父瓦片与父瓦片是否存在。

    不同字节的转码副本在这里被显式标注为转码，调用方不得把它
    当作额外覆盖。
    """
    tiles = list(tiles)
    by_id = {t['upload_id']: t for t in tiles}
    chain = []
    for tile in tiles:
        if not is_transcode(tile):
            continue
        parent_id = tile.get('transcode_of')
        parent = by_id.get(parent_id)
        chain.append({
            'upload_id': tile['upload_id'],
            'sha': tile.get('sha'),
            'transcode_of': parent_id,
            'parent_present': parent is not None,
            'parent_tier': source_tier(parent) if parent else None,
            'same_bytes_as_parent': bool(parent and tile.get('sha') == parent.get('sha')),
        })
    return chain


# ---------------------------------------------------------------------------
# 采录包解析与校验
# ---------------------------------------------------------------------------

def parse_bundle(payload: Any) -> dict:
    """兼容两种输入：新页级采录包，或旧的裸瓦片行列表。"""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {'schema': 'page-tile-bundle/legacy-rows', 'page': {}, 'tiles': payload}
    raise TileCaptureError('采录包必须是页级对象或瓦片行列表')


def validate(bundle: dict) -> dict:
    """整包校验，返回规范化后的 bundle（不修改输入）。

    校验：翻页方向、坐标网格、每瓦坐标转换与 footprint、网格配准、
    区域标签、转码父链、页级封存清单与瓦片哈希的一致性。
    """
    bundle = parse_bundle(bundle)
    page = bundle.get('page') or {}
    tiles = list(bundle.get('tiles', ()))

    ids = [t.get('upload_id') for t in tiles]
    if any(not i for i in ids):
        raise TileCaptureError('存在缺少 upload_id 的瓦片')
    if len(set(ids)) != len(ids):
        raise TileCaptureError('upload_id 重复，无法作为去重键')

    has_grid = bool(page.get('page_width_mm') and page.get('page_height_mm'))
    turn = page.get('page_turn', 'unknown')
    if turn not in PAGE_TURNS:
        raise TileCaptureError(f'翻页方向未知: {turn!r}')

    by_id = {t['upload_id']: t for t in tiles}
    for tile in tiles:
        tier = source_tier(tile)
        if not has_grid:
            # 旧格式行没有坐标网格，无法做物理配准，只保留计数语义。
            continue
        fp = footprint_from_transform(tile)
        if not contains(rect(0.0, 0.0, page['page_width_mm'], page['page_height_mm']), fp):
            raise TileCaptureError(f"瓦片 {tile['upload_id']!r} 越出纸页坐标范围")
        matched = cell_of_footprint(page, fp)
        declared = tile.get('grid_cell')
        if declared is not None and declared not in GRID_CELLS:
            raise TileCaptureError(f"瓦片 {tile['upload_id']!r} 网格单元非法: {declared!r}")
        if declared is not None and matched != declared:
            raise TileCaptureError(
                f"瓦片 {tile['upload_id']!r} 声明 {declared} 但坐标实际落在 {matched}"
            )
        region = tile.get('region')
        if region is not None and region in KNOWN_REGIONS and region != matched:
            raise TileCaptureError(
                f"瓦片 {tile['upload_id']!r} region={region} 与坐标系配准结果 {matched} 冲突"
            )
        if tier == TIER_TRANSCODE:
            parent_id = tile.get('transcode_of')
            if not parent_id or parent_id not in by_id:
                raise TileCaptureError(
                    f"转码瓦片 {tile['upload_id']!r} 缺少可核验的父影像 transcode_of"
                )
            if not contains(by_id[parent_id]['footprint'], fp):
                raise TileCaptureError(
                    f"转码瓦片 {tile['upload_id']!r} 的 footprint 超出父影像区域"
                )

    problems = seal_manifest_problems(bundle)
    return {**bundle, 'page': page, 'tiles': tiles, 'validation_problems': problems}


# ---------------------------------------------------------------------------
# 覆盖地图与缺图清单
# ---------------------------------------------------------------------------

def _coordinate_tiles(bundle: dict) -> list[dict]:
    page = bundle['page']
    if not (page.get('page_width_mm') and page.get('page_height_mm')):
        raise TileCaptureError('缺少扫描仪原始坐标网格，无法按物理区域配准')
    return list(bundle['tiles'])


def coverage_map(bundle: dict) -> dict:
    """可复核的页图覆盖地图：按同一纸页坐标系逐网格单元配准。

    转码瓦片不进入任何覆盖集合；辅助补拍单独成层，不能与扫描仪
    原件混成同一来源等级。
    """
    bundle = parse_bundle(bundle)
    page = bundle['page']
    cells = grid_cells(page) if page.get('page_width_mm') else {}
    raw_cells, aux_cells, transcode_only = [], [], []
    raw_footprints, aux_footprints = [], []
    for tile in _coordinate_tiles(bundle):
        tier = source_tier(tile)
        fp = footprint_from_transform(tile)
        if tier == TIER_TRANSCODE:
            continue
        covered = [name for name, cell in cells.items() if intersects(fp, cell)]
        if tier == TIER_RAW:
            raw_footprints.append(fp)
            for name in covered:
                if name not in raw_cells:
                    raw_cells.append(name)
        else:
            aux_footprints.append(fp)
            for name in covered:
                if name not in aux_cells:
                    aux_cells.append(name)
    for name in GRID_CELLS:
        if name not in raw_cells and name not in aux_cells:
            transcode_only.append(name)
    covered = [c for c in GRID_CELLS if c in raw_cells or c in aux_cells]
    return {
        'page_id': page.get('page_id'),
        'coordinate_system': 'scanner-raw-mm',
        'page_turn': page.get('page_turn', 'unknown'),
        'grid': cells,
        'covered_cells': [c for c in GRID_CELLS if c in covered],
        'raw_cells': [c for c in GRID_CELLS if c in raw_cells],
        'auxiliary_cells': [c for c in GRID_CELLS if c in aux_cells],
        'transcode_only_cells': transcode_only,
        'transcodes_excluded': [c['upload_id'] for c in transcode_chain(bundle['tiles'])],
        'raw_union': union(raw_footprints),
        'auxiliary_union': union(aux_footprints),
    }


def physical_region_union(bundle: dict) -> dict | None:
    """可验证覆盖（原始 + 辅助，排除转码）的物理区域并集。"""
    bundle = parse_bundle(bundle)
    fps = [
        footprint_from_transform(t)
        for t in _coordinate_tiles(bundle)
        if not is_transcode(t)
    ]
    return union(fps)


def missing_list(bundle: dict) -> list[dict]:
    """缺图清单：未扫描与仅辅助覆盖分开列明。

    - ``missing``     该网格单元没有任何可验证影像；
    - ``raw_missing`` 仅有独立相机辅助补拍，缺扫描仪原始瓦片。
    遮挡与折边是页级标注，不在此处与缺图混淆。
    """
    bundle = parse_bundle(bundle)
    cmap = coverage_map(bundle)
    out = []
    for cell in GRID_CELLS:
        if cell in cmap['raw_cells']:
            continue
        if cell in cmap['auxiliary_cells']:
            reason = 'raw_missing'
            note = '仅辅助来源覆盖，缺扫描仪原始瓦片'
        else:
            reason = 'missing'
            note = '未扫描区域，无任何可验证影像（转码副本不计覆盖）'
        out.append({
            'page_id': cmap['page_id'],
            'grid_cell': cell,
            'reason': reason,
            'note': note,
            'footprint': cmap['grid'][cell],
        })
    return out


def occluded_regions(bundle: dict) -> list[dict]:
    """遮挡区域（单独列明，不与未扫描、折边混算）。"""
    bundle = parse_bundle(bundle)
    return [dict(item) for item in (bundle.get('occlusions') or ())]


def folded_edges(bundle: dict) -> list[dict]:
    """折边区域（单独列明）。"""
    bundle = parse_bundle(bundle)
    return [dict(item) for item in (bundle.get('folded_edges') or ())]


# ---------------------------------------------------------------------------
# 页级封存清单
# ---------------------------------------------------------------------------

def seal_manifest_problems(bundle: dict) -> list[str]:
    """核对页级封存清单与瓦片哈希，返回问题（空列表表示一致）。"""
    bundle = parse_bundle(bundle)
    problems: list[str] = []
    manifest = bundle.get('seal_manifest')
    tiles = list(bundle.get('tiles', ()))
    by_id = {t['upload_id']: t for t in tiles}
    if manifest is None:
        return ['缺少页级封存清单']
    if not manifest.get('sealed'):
        problems.append('封存清单未签封')
    entries = manifest.get('entries', [])
    if not entries:
        problems.append('封存清单为空')
    seen = set()
    for entry in entries:
        uid = entry.get('upload_id')
        seen.add(uid)
        tile = by_id.get(uid)
        if tile is None:
            problems.append(f'封存清单引用了不存在的瓦片: {uid}')
            continue
        if entry.get('sha') != tile.get('sha'):
            raise TileCaptureError(f'瓦片 {uid} 封存哈希与采录哈希不一致')
        if entry.get('tier') != source_tier(tile):
            problems.append(f'瓦片 {uid} 封存来源等级与采录不一致')
    for tile in tiles:
        if source_tier(tile) == TIER_RAW and tile['upload_id'] not in seen:
            problems.append(f"扫描仪原始瓦片 {tile['upload_id']} 未进入封存清单")
    return problems


# ---------------------------------------------------------------------------
# 迟到瓦片与上传重试
# ---------------------------------------------------------------------------

def register_late_tile(bundle: dict, tile: dict) -> dict:
    """把迟到图块登记到同一物理页；跨页拒绝、重复 upload_id 幂等。

    返回新的采录包，不修改输入。迟到瓦片仅修订其所属物理页，
    绝不能并到别的纸页。
    """
    bundle = parse_bundle(bundle)
    page_id = bundle.get('page', {}).get('page_id')
    if page_id and tile.get('page_id') and tile['page_id'] != page_id:
        raise TileCaptureError(
            f"迟到瓦片属于 {tile['page_id']}，不能修订物理页 {page_id}"
        )
    tiles = list(bundle['tiles'])
    for existing in tiles:
        if existing['upload_id'] == tile['upload_id']:
            if existing.get('sha') != tile.get('sha'):
                raise TileCaptureError('同一 upload_id 出现不同内容，拒绝覆盖')
            return validate(bundle)  # 上传重试：幂等，不重复计数
    tiles.append(tile)
    return validate({**bundle, 'tiles': tiles})


# ---------------------------------------------------------------------------
# 兼容入口
# ---------------------------------------------------------------------------

def uploads(rows) -> int:
    """上传去重计数（仅数 upload_id）。

    保留该函数是为了显式证明：三次上传不等于三个物理区域被扫描，
    完整性判定必须改用 :func:`coverage_map` / :func:`missing_list`。
    """
    bundle = parse_bundle(rows)
    return len({row['upload_id'] for row in bundle['tiles']})
