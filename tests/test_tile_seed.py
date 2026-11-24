import copy
import json
import unittest
from pathlib import Path

from bookconserve import page_archive
from bookconserve import tile_capture as tc
from bookconserve.page_archive import assess, legacy_assess
from bookconserve.tile_capture import uploads

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'page-tiles.json'


def load_bundle():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class TileSeedTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle()

    # -- 旧错误报告必须可复现，并与新裁定并列可见 -------------------------
    def test_upload_count_still_overstates_physical_coverage(self):
        rows = self.bundle
        self.assertEqual(uploads(rows), 3)
        # 旧规则（只数 upload_id）继续复现错误“完整”报告。
        self.assertEqual(legacy_assess(rows), {'uploads': 3, 'status': '完整'})
        # 物理区域只有两块，且底部两块完全没有任何图块。
        cmap = tc.coverage_map(rows)
        self.assertEqual(
            sorted(cmap['raw_cells']), ['top-left', 'top-right']
        )
        self.assertEqual(
            sorted(cmap['transcode_only_cells']),
            ['bottom-left', 'bottom-right'],
        )
        self.assertEqual(cmap['transcodes_excluded'], ['tile-c'])

    def test_new_assessment_marks_page_missing_with_gap_list(self):
        result = assess(self.bundle)
        self.assertEqual(result['uploads'], 3)
        self.assertEqual(result['status'], '缺图')
        self.assertEqual(
            sorted(result['unscanned_cells']), ['bottom-left', 'bottom-right']
        )
        self.assertEqual(
            [m['grid_cell'] for m in result['missing_list']],
            ['bottom-left', 'bottom-right'],
        )
        # 新报告同时保留旧错误报告，便于馆方复核。
        self.assertEqual(result['legacy_report']['uploads'], 3)
        self.assertEqual(result['legacy_report']['status'], '完整')

    # -- 转码副本：不同字节也不能冒作额外覆盖 -----------------------------
    def test_distinct_bytes_transcode_adds_no_coverage(self):
        chain = tc.transcode_chain(self.bundle['tiles'])
        self.assertEqual(len(chain), 1)
        link = chain[0]
        self.assertEqual(link['upload_id'], 'tile-c')
        self.assertEqual(link['transcode_of'], 'tile-b')
        self.assertTrue(link['parent_present'])
        self.assertFalse(link['same_bytes_as_parent'])  # 哈希确实不同
        # 转码前后物理并集完全相同。
        without_c = {**self.bundle, 'tiles': self.bundle['tiles'][:2]}
        self.assertEqual(
            tc.physical_region_union(self.bundle),
            tc.physical_region_union(without_c),
        )
        self.assertEqual(
            tc.physical_region_union(self.bundle),
            {'x0': 0.0, 'y0': 0.0, 'x1': 300.0, 'y1': 200.0},
        )

    def test_transcode_footprint_outside_parent_is_rejected(self):
        bad = copy.deepcopy(self.bundle)
        bad['tiles'][2]['footprint'] = {
            'x0': 0.0, 'y0': 200.0, 'x1': 150.0, 'y1': 400.0
        }
        bad['tiles'][2]['transform'] = [0.25, 0.25, 0.0, 200.0]
        with self.assertRaises(tc.TileCaptureError):
            tc.validate(bad)

    # -- 图像坐标转换与同页配准 ------------------------------------------
    def test_footprint_derived_from_pixel_transform(self):
        tile = self.bundle['tiles'][0]
        self.assertEqual(
            tc.footprint_from_transform(tile),
            {'x0': 0.0, 'y0': 0.0, 'x1': 150.0, 'y1': 200.0},
        )

    def test_declared_footprint_must_match_transform(self):
        bad = copy.deepcopy(self.bundle)
        bad['tiles'][0]['footprint']['x1'] = 149.0
        with self.assertRaises(tc.TileCaptureError):
            tc.footprint_from_transform(bad['tiles'][0])

    def test_filename_region_cannot_override_coordinate_registration(self):
        # 文件名/region 声称 bottom-left，但坐标仍在 top-right：拒绝。
        bad = copy.deepcopy(self.bundle)
        bad['tiles'][1]['region'] = 'bottom-left'
        with self.assertRaises(tc.TileCaptureError):
            tc.validate(bad)

    def test_legacy_rows_without_grid_cannot_be_complete(self):
        legacy_rows = [
            {'upload_id': 'tile-a', 'device': 'scanner', 'region': 'top-left', 'sha': 'raw-a'},
            {'upload_id': 'tile-b', 'device': 'scanner', 'region': 'top-right', 'sha': 'raw-b'},
            {'upload_id': 'tile-c', 'device': 'proxy', 'region': 'top-right', 'sha': 'jpeg-c'},
        ]
        self.assertEqual(uploads(legacy_rows), 3)
        result = assess(legacy_rows)
        self.assertEqual(result['status'], '缺图')
        self.assertIn('坐标网格', result['reason'])

    # -- 遮挡、折边与未扫描分开列明 ---------------------------------------
    def test_occlusion_fold_and_unscanned_are_separate(self):
        result = assess(self.bundle)
        self.assertEqual(len(result['occluded_regions']), 1)
        self.assertEqual(result['occluded_regions'][0]['region'], 'top-left')
        self.assertEqual(len(result['folded_edges']), 1)
        self.assertEqual(result['folded_edges'][0]['region'], 'top-left')
        # 遮挡/折边位于已覆盖单元，不改变缺图清单；缺图只认未扫描。
        self.assertEqual(
            sorted(result['unscanned_cells']), ['bottom-left', 'bottom-right']
        )

    # -- 补录路径 A：找回扫描仪底部原始瓦片 + 校准板 → 整页原始证书 -------
    def test_path_a_recovered_raw_tiles_yield_full_raw_certificate(self):
        amended = page_archive.supplement_raw_tile(
            self.bundle,
            upload_id='tile-d',
            sha='raw-d',
            cells=['bottom-left', 'bottom-right'],
            board_signature='calib-board-2026-11',
        )
        result = assess(amended)
        self.assertEqual(result['status'], '完整')
        self.assertEqual(result['unscanned_cells'], [])
        self.assertEqual(result['raw_missing_cells'], [])
        self.assertEqual(
            result['coverage_map']['raw_cells'], list(tc.GRID_CELLS)
        )
        # tile-c 转码链与旧错误报告仍然保留。
        self.assertEqual(
            [c['upload_id'] for c in result['transcode_chain']], ['tile-c']
        )
        self.assertEqual(result['legacy_report']['status'], '完整')
        cert = page_archive.raw_page_certificate(amended)
        self.assertTrue(cert['legal'])
        self.assertEqual(cert['source_tier'], 'raw')
        self.assertEqual(cert['scope'], list(tc.GRID_CELLS))
        self.assertEqual(cert['pending_cells'], [])
        # 封存清单纳入新原始瓦片且哈希一致。
        sealed = {e['upload_id'] for e in result['seal_manifest']['entries']}
        self.assertIn('tile-d-bottom-left', sealed)
        self.assertIn('tile-d-bottom-right', sealed)
        self.assertEqual(result['seal_problems'], [])

    def test_path_a_without_calibration_board_is_not_complete(self):
        amended = page_archive.supplement_raw_tile(
            self.bundle, 'tile-d', 'raw-d',
            ['bottom-left', 'bottom-right'], 'calib-board-2026-11',
        )
        amended['page']['calibration_board']['present'] = False
        result = assess(amended)
        self.assertEqual(result['status'], '原始覆盖齐备待校准签封')
        self.assertFalse(page_archive.raw_page_certificate(amended)['legal'])

    # -- 补录路径 B：仅独立相机补拍左下 → 辅助证书，右下仍缺图 ------------
    def test_path_b_auxiliary_shot_cannot_claim_raw_or_full_page(self):
        amended = page_archive.supplement_auxiliary_tile(
            self.bundle, upload_id='tile-e', sha='aux-e', cell_name='bottom-left'
        )
        result = assess(amended)
        self.assertEqual(result['status'], '辅助覆盖待原始补扫')
        self.assertEqual(result['raw_missing_cells'], ['bottom-left'])
        self.assertEqual(result['unscanned_cells'], ['bottom-right'])
        # 来源等级分层，不与扫描仪原件混级。
        self.assertEqual(result['coverage_map']['auxiliary_cells'], ['bottom-left'])
        self.assertNotIn('bottom-left', result['coverage_map']['raw_cells'])
        cert = page_archive.auxiliary_certificate(amended, 'bottom-left')
        self.assertTrue(cert['legal'])
        self.assertEqual(cert['source_tier'], 'auxiliary')
        self.assertEqual(cert['scope'], ['bottom-left'])
        self.assertEqual(cert['pending_cells'], ['bottom-right', 'bottom-left'])
        # 辅助照片不得进入扫描仪原始封存清单。
        sealed = {e['upload_id'] for e in result['seal_manifest']['entries']}
        self.assertNotIn('tile-e', sealed)
        # 不能给整页原始扫描证书。
        self.assertFalse(page_archive.raw_page_certificate(amended)['legal'])

    def test_auxiliary_certificate_rejects_cell_without_aux_tile(self):
        cert = page_archive.auxiliary_certificate(self.bundle, 'bottom-right')
        self.assertFalse(cert['legal'])
        self.assertEqual(cert['scope'], [])

    # -- 迟到图块只修订同一物理页；上传重试幂等 ---------------------------
    def test_late_tile_rejects_other_physical_page(self):
        foreign = {
            'upload_id': 'tile-x', 'page_id': 'V1-P08', 'device': 'scanner',
            'tier': 'raw', 'grid_cell': 'bottom-left', 'region': 'bottom-left',
            'sha': 'raw-x', 'pixel_width': 600, 'pixel_height': 800,
            'transform': [0.25, 0.25, 0.0, 200.0],
            'footprint': {'x0': 0.0, 'y0': 200.0, 'x1': 150.0, 'y1': 400.0},
        }
        with self.assertRaises(tc.TileCaptureError):
            tc.register_late_tile(self.bundle, foreign)

    def test_retry_same_upload_id_is_idempotent_and_not_double_counted(self):
        tile = {
            'upload_id': 'tile-a', 'page_id': 'V1-P07', 'device': 'scanner',
            'tier': 'raw', 'grid_cell': 'top-left', 'region': 'top-left',
            'sha': 'raw-a', 'pixel_width': 600, 'pixel_height': 800,
            'transform': [0.25, 0.25, 0.0, 0.0],
            'footprint': {'x0': 0.0, 'y0': 0.0, 'x1': 150.0, 'y1': 200.0},
        }
        before = uploads(self.bundle)
        amended = tc.register_late_tile(self.bundle, tile)
        self.assertEqual(uploads(amended), before)
        with self.assertRaises(tc.TileCaptureError):
            tc.register_late_tile(
                self.bundle, {**tile, 'sha': 'tampered'}
            )

    # -- 页级封存清单与哈希 ----------------------------------------------
    def test_seal_manifest_hash_tamper_is_detected(self):
        bad = copy.deepcopy(self.bundle)
        bad['seal_manifest']['entries'][0]['sha'] = 'wrong'
        with self.assertRaises(tc.TileCaptureError):
            tc.seal_manifest_problems(bad)

    def test_missing_manifest_and_unsigned_manifest_are_problems(self):
        no_manifest = {k: v for k, v in self.bundle.items() if k != 'seal_manifest'}
        self.assertIn('缺少页级封存清单', tc.seal_manifest_problems(no_manifest))
        unsigned = copy.deepcopy(self.bundle)
        unsigned['seal_manifest']['sealed'] = False
        self.assertIn('封存清单未签封', tc.seal_manifest_problems(unsigned))

    # -- 翻页方向与坐标网格元数据 -----------------------------------------
    def test_page_turn_and_grid_metadata_recorded_offline(self):
        self.assertEqual(self.bundle['page']['page_turn'], 'right-to-left')
        cells = tc.grid_cells(self.bundle['page'])
        self.assertEqual(set(cells), set(tc.GRID_CELLS))
        self.assertEqual(
            cells['bottom-right'],
            {'x0': 150.0, 'y0': 200.0, 'x1': 300.0, 'y1': 400.0},
        )
        bad = copy.deepcopy(self.bundle)
        bad['page']['page_turn'] = 'inside-out'
        with self.assertRaises(tc.TileCaptureError):
            tc.validate(bad)


if __name__ == '__main__':
    unittest.main()
