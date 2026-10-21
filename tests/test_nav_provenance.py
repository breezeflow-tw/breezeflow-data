"""不以網站交易日冒充資料日期；淨值與其來源快照不可拆開沿用。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fetch_etf as f
import nav_provenance as n
import store


class NavProvenanceTest(unittest.TestCase):
    def test_invalid_timestamps_do_not_become_dates(self):
        for value in (None,"20261021","2026-10-21 13:30:00","20260230 13:30:00","20261021 25:00:00","20261021 1:2:3"):
            self.assertIsNone(n.timestamp_day(value))
        self.assertEqual(n.timestamp_day("20261020 13:30:00"),"2026-10-20")

    def test_restore_requires_same_source_and_matching_official_value(self):
        with tempfile.TemporaryDirectory() as td:
            con=store.open_db(Path(td)/"a.db",None)
            rows=[("00991",10,10),("00992",10,11),("00993",10,None),("00994",10,None)]
            con.executemany("INSERT INTO daily(date,code,nav,nav_official) VALUES('2026-10-21',?,?,?)",rows)
            items=[{"code":c,"nav":v,"nav_source":"投信投顧公會（正式淨值）"} for c,v,_ in rows[:2]]
            items+=[{"code":"00993","nav":10,"nav_source":"證交所（投信預估淨值）","source_time":"20261020 13:30:00"},
                    {"code":"00994","nav":10,"nav_source":"未核對來源","source_time":"20261021 13:30:00"}]
            n.restore(con,items,"2026-10-21")
            self.assertEqual(items[0]["nav_date"],"2026-10-21")
            self.assertNotIn("nav_date",items[1])
            self.assertEqual(items[2]["nav_date"],"2026-10-20")
            self.assertNotIn("nav_date",items[3])
            con.close()

    def test_rerun_keeps_value_date_url_together_and_new_value_clears_old_date(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/"a.db"
            def item():
                rows=f.merge({}, {"00999B":{"name":"測試債券","close":10,"market":"上櫃"}}, {}, "2026-10-21")
                f.apply_issuer(rows,{},"2026-10-21")
                return rows[0]
            first=item();first.update(nav=10,nav_source="測試投信官網（正式淨值）",nav_date="2026-10-20",nav_source_url="https://example.invalid/nav",source_time="20261020 13:30:00")
            f.save_db([first],"2026-10-21",db)
            missing=item();f.save_db([missing],"2026-10-21",db)
            for key in ("nav","nav_source","nav_date","nav_source_url","source_time"):
                self.assertEqual(missing[key],first[key],key)
            new=item();new.update(nav=12,nav_source="證交所（投信預估淨值）",source_time=None)
            f.save_db([new],"2026-10-21",db)
            self.assertIsNone(new["nav_date"])
            self.assertIsNone(new["nav_source_url"])
            self.assertIsNone(new["source_time"])
            con=store.open_db(db,None);store.dump(con,Path(td)/"history");con.close()
            con=store.open_db(Path(td)/"b.db",Path(td)/"history")
            self.assertEqual(con.execute("SELECT nav, nav_date, nav_source_url FROM daily").fetchone(),(12,None,None))
            con.close()


if __name__ == "__main__":
    unittest.main()
