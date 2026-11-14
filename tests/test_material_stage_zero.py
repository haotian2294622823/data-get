"""通过原始 CSV 验证工序0和最终分片拼接，无需数据库。"""

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lot_extract.final_export import column_rank, join_parts
from lot_extract.material_merge import load_export_rules
from lot_extract.merge import merge_tables, write_wide
from lot_extract.tables import (
    DEFECT, LOT, MACHINE, MATERIAL, PROCESS, TABLES, UPSTREAM_PREFIX, UPSTREAM_TABLES,
    note_inheritance,
)


EXTRA = {
    LOT: ('batch_note',),
    PROCESS: ('pass_order', 'process_name', 'temperature', 'empty_parameter', 'new_parameter',
              'seiku', 'actual_start_datetime'),
    DEFECT: ('defect_name', 'defect_qty'),
    MACHINE: ('machine_name', 'seiku', 'actual_start_datetime'),
    MATERIAL: ('material_lot_no',),
}


def write_tables(path, sources, records, prefix=''):
    path.mkdir(parents=True, exist_ok=True)
    for source in sources:
        fields = [*TABLES[source][1], *EXTRA.get(source, ())]
        with (path / f'{prefix}{source}.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records.get(source, []))


def engineering(lot, key, name, temperature='0', **fields):
    return dict(lot_no=lot, process_key=key, process_name=name, pass_order='1', temperature=temperature, **fields)


def machine(lot, key, name, order='1.0'):
    return dict(lot_no=lot, process_key=key, pass_order=order, machine_code='01', machine_name=name)


def defect(lot, key, name):
    return dict(lot_no=lot, process_key=key, pass_order='1', defect_code=f'D{name}', defect_name=name, defect_qty='2')


def fixture(path, usages, processes, *, equipment=(), defects=(), missing_lots=()):
    system = {LOT: [], PROCESS: [], MATERIAL: [], MACHINE: [], DEFECT: []}
    for lot, materials in usages.items():
        system[LOT].append(dict(lot_no=lot))
        system[PROCESS].append(engineering(lot, 'SYS', '制造'))
        system[MACHINE].append(machine(lot, 'SYS', '制造机台'))
        system[DEFECT].append(defect(lot, 'SYS', '制造不良'))
        for number, material in enumerate(materials, 1):
            system[MATERIAL].append(dict(lot_no=lot, process_key='SYS', pass_order='1',
                                         seq=str(number), material_lot_no=material))
    all_materials = dict.fromkeys(material for materials in usages.values() for material in materials)
    upstream = {
        LOT: [dict(lot_no=lot, batch_note='原始批次属性') for lot in all_materials if lot not in missing_lots],
        PROCESS: processes, MACHINE: equipment, DEFECT: defects,
    }
    write_tables(path, TABLES, system)
    write_tables(path, UPSTREAM_TABLES, upstream, UPSTREAM_PREFIX)


class MaterialStageZeroTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_winding_groups_use_distinct_material_lots_and_attach_correct_details(self):
        fixture(self.root, {'E100': ['M200', 'M200', 'M201'], 'E101': ['M200']}, [
            engineering('M200', 'W', '卷线前工程', '12'), engineering('M201', 'W', 'Winding', '14'),
            engineering('M200', 'E1', 'E Core'), engineering('M200', 'E2', 'E-Core 成型'),
            engineering('M200', 'I1', 'ICORE'),
        ], equipment=[machine('M200', 'W', '机台A'), machine('M201', 'W', '机台B'),
                      machine('M200', 'E1', '磁芯机台')],
           defects=[defect('M200', 'W', '断线'), defect('M201', 'W', '偏移'), defect('M200', 'I1', '裂纹')])
        columns, rows, _ = merge_tables(self.root)
        self.assertEqual([row['lot_no'] for row in rows], ['E100', 'E101'])
        first, second = rows
        for number, lot, device, flaw in [(1, 'M200', '机台A', '断线'), (2, 'M201', '机台B', '偏移')]:
            prefix = f'工序0_卷线{number}_'
            self.assertEqual(first.get(prefix + 'material_lot_no'), lot)
            self.assertEqual(first[prefix + '机台设备1_machine_name'], device)
            self.assertEqual(first[prefix + f'不良_代码D{flaw}'], '2')
            self.assertEqual(first[prefix + f'不良_代码D{flaw}_名称'], flaw)
        self.assertNotIn('工序0_卷线2_material_lot_no', second)
        self.assertFalse(any(c.startswith('工序0_卷线3_') for c in columns))
        self.assertEqual(first['工序0_E-Core2_process_key'], 'E2')
        self.assertEqual(first['工序0_I-Core1_process_key'], 'I1')
        self.assertEqual(first['工序0_E-Core1_机台设备1_machine_name'], '磁芯机台')
        self.assertEqual(first['工序0_I-Core1_不良_代码D裂纹'], '2')
        self.assertEqual(first['工序1_机台设备1_machine_name'], '制造机台')
        self.assertEqual(first['工序1_不良_代码D制造不良'], '2')

    def test_additional_execution_stays_in_same_winding_group_and_orphans_are_visible(self):
        fixture(self.root, {'E100': ['M200', 'M201']}, [
            engineering('M200', 'A', '卷线'), engineering('M200', 'B', '卷线'), engineering('M201', 'A', '卷线'),
        ], equipment=[machine('M200', 'B', '第二次设备'), machine('M200', 'A', '道次不符', '2')],
           missing_lots=['M200'])
        _, [row], notices = merge_tables(self.root)
        self.assertEqual(row.get('工序0_卷线1_执行2_机台设备1_machine_name'), '第二次设备')
        self.assertEqual(row['工序0_卷线2_material_lot_no'], 'M201')
        self.assertEqual(row['工序0_未匹配工程1_机台设备1_machine_name'], '道次不符')
        self.assertTrue(notices)

    def test_dynamic_columns_match_between_whole_and_split_exports(self):
        usages = {'E100': ['M00', 'C00'],
                  'E101': ['M01', 'M00', *[f'M{i:02}' for i in range(2, 11)], *[f'C{i:02}' for i in range(11)]]}
        processes = [engineering(f'M{i:02}', 'W', '卷线', seiku='Y26', actual_start_datetime='08:00')
                     for i in range(11)]
        processes += [engineering(f'C{i:02}', 'E', 'E-Core', new_parameter='007' if i == 1 else '') for i in range(11)]
        equipment = [
            dict(machine('M00', 'W', '机台A'), seiku='Y26', actual_start_datetime='08:00'),
            dict(machine('M01', 'W', '机台B'), seiku='', actual_start_datetime='08:00'),
        ]
        defects = [
            dict(defect('M00', 'W', '断线'), defect_code='01', defect_qty='2.000'),
            dict(defect('M01', 'W', '断线旧称'), defect_code='01', defect_qty='2.500'),
        ]
        full_path = self.root / 'whole'
        fixture(full_path, usages, processes, equipment=equipment, defects=defects)
        columns, rows, _ = merge_tables(full_path)
        self.assertNotIn('工序0_卷线1_机台设备1_actual_start_datetime', columns)
        self.assertEqual(rows[0]['工序0_卷线1_机台设备1_seiku'], 'Y26')
        self.assertEqual(rows[1]['工序0_卷线1_机台设备1_seiku'], '')
        self.assertEqual(rows[1]['工序0_卷线1_不良_代码01'], '2.5')
        self.assertEqual(rows[1]['工序0_卷线1_不良_代码01_名称'], '断线旧称')
        self.assertIn('工序0_E-Core11_material_lot_no', columns)
        self.assertEqual(rows[1]['工序0_E-Core2_new_parameter'], '007')
        self.assertEqual(rows[0]['工序0_卷线1_temperature'], '0')
        self.assertFalse(any('empty_parameter' in col or 'batch_note' in col
                             for col in columns if col.startswith('工序0_')))
        self.assertLess(columns.index('工序0_卷线2_material_lot_no'), columns.index('工序0_卷线10_material_lot_no'))
        parts = []
        inheritance = {}
        rules = load_export_rules()
        for index, (lot, materials) in enumerate(usages.items()):
            directory = self.root / str(index)
            fixture(directory, {lot: materials}, [p for p in processes if p['lot_no'] in materials],
                    equipment=[row for row in equipment if row['lot_no'] in materials],
                    defects=[row for row in defects if row['lot_no'] in materials])
            evidence = {}
            part_columns, part_rows, _ = merge_tables(directory, rules=rules, inheritance=evidence)
            for column, parent in evidence.items():
                note_inheritance(inheritance, column, parent)
            file = f'{index}.csv'
            with (self.root / file).open('w', encoding='utf-8', newline='') as stream:
                write_wide(stream, part_columns, part_rows)
            parts.append(dict(file=file, rows=1))
        join_parts(self.root, parts, lambda: None, lambda *_: None, rules=rules, inheritance=inheritance)
        with (self.root / 'final.csv').open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, columns)
            self.assertLess(reader.fieldnames.index('工序0_卷线10_material_lot_no'),
                            reader.fieldnames.index('工序1_process_key'))
            self.assertEqual(list(reader), [{c: row.get(c, '') for c in columns} for row in rows])

    def test_manufacturing_pass_zero_is_distinct_from_material_stage(self):
        self.assertLess(column_rank('工序0_卷线1_material_lot_no', 5), column_rank('工序0_材料1_seq', 0))

    def test_name_containing_an_alias_uses_unknown_group(self):
        fixture(self.root, {'E100': ['M200']}, [engineering('M200', 'W', 'Unwinding')])
        _, [row], notices = merge_tables(self.root)
        self.assertEqual(row.get('工序0_未识别工程1_process_name'), 'Unwinding')
        self.assertFalse(any(c.startswith('工序0_卷线') for c in row))
        self.assertTrue(notices)

    def test_high_precision_pass_is_not_rounded_for_matching(self):
        fixture(self.root, {'E100': ['M200']}, [engineering('M200', 'W', '卷线')], equipment=[
            machine('M200', 'W', '正确设备'),
            machine('M200', 'W', '另一道次', '1.00000000000000000000000000001'),
        ])
        _, [row], _ = merge_tables(self.root)
        self.assertEqual(row['工序0_卷线1_机台设备1_machine_name'], '正确设备')
        self.assertEqual(row['工序0_未匹配工程1_机台设备1_machine_name'], '另一道次')

    def test_old_material_columns_still_sort_before_manufacturing(self):
        with (self.root / 'old.csv').open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows([['lot_no', '工序1_process_key', '工序0-卷线_1_material_lot_no'],
                                         ['E100', 'P1', 'M200']])
        join_parts(self.root, [dict(file='old.csv', rows=1)], lambda: None, lambda *_: None)
        with (self.root / 'final.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(next(csv.reader(stream)), ['lot_no', '工序0-卷线_1_material_lot_no', '工序1_process_key'])
