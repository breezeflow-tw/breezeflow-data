"""週資金流與公會月份資料的離線測試，數值均為合成樣本。"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fund_flows as f
import sitca
import store


def rows():
    out, units = {}, 100_000_000
    for i in range(29):
        d = date(2026, 9, 18) + timedelta(days=i)
        if d.weekday() > 4:
            continue
        if out:
            units += 1_000_000
        out[d.isoformat()] = {'code': 'TEST', 'date': d.isoformat(), 'nav': 10,
            'units': units, 'units_change': 1_000_000, 'aum': units*10/1e8, 'currency': 'TWD',
            'source': '測試投信官網', 'source_url': 'https://www.yuantaetfs.com/test'}
    return out


class FlowTest(unittest.TestCase):
    def test_amount_rate_units_and_four_weeks(self):
        r = f.week(rows(), date(2026,10,12), date(2026,10,16))
        self.assertEqual(r['net_amount'], .5)
        self.assertAlmostEqual(r['net_rate'], .5/11.5*100, places=4)
        self.assertEqual(r['net_rate'], r['units_rate'])
        self.assertEqual(r['aum_change'], .5)
        con = sqlite3.connect(':memory:'); store.ensure_schema(con)
        for r in rows().values(): f.upsert(con,'fund_daily',('code','date'),r)
        items = [{'code':'TEST'}]; f.attach(con,items,'2026-10-16')
        self.assertEqual(items[0]['flows']['four_week_amount'], 2)
        self.assertEqual(items[0]['flows']['positive_weeks'], 4)
        self.assertEqual(items[0]['units_date'],'2026-10-16')
        con.close()

    def test_zero_is_not_missing_and_aum_change_is_not_flow(self):
        data=rows()
        for r in data.values(): r.update(units=100_000_000,units_change=0)
        data['2026-10-16'].update(nav=11,aum=11)
        r=f.week(data,date(2026,10,12),date(2026,10,16))
        self.assertEqual(r['net_amount'],0)
        self.assertEqual(r['aum_change'],-.5)
        self.assertEqual(r['units_rate'],0)

    def test_redemption(self):
        data=rows()
        for d,r in data.items():
            r['units']=200_000_000-r['units'];r['units_change']=-1_000_000
        self.assertEqual(f.week(data,date(2026,10,12),date(2026,10,16))['net_amount'],-.5)

    def test_missing_weekday_does_not_become_zero_or_holiday(self):
        data=rows();del data['2026-10-13']
        r=f.week(data,date(2026,10,12),date(2026,10,16))
        self.assertIsNone(r['net_amount']);self.assertIn('2026-10-13',r['reason'])

    def test_missing_baseline_and_unconfirmed_currency(self):
        for change in ('baseline','currency','nav','units_change','source'):
            with self.subTest(change=change):
                data=rows()
                if change=='baseline': del data['2026-10-09']
                else: data['2026-10-13'][change]={'currency':'USD','nav':None,'units_change':3,'source':'另一來源'}[change]
                self.assertIsNone(f.week(data,date(2026,10,12),date(2026,10,16))['net_amount'])

    def test_split_and_other_events_are_not_inflow(self):
        events=[{'date':'2026-10-13','kind':'分割'}]
        self.assertIsNone(f.week(rows(),date(2026,10,12),date(2026,10,16),events)['net_amount'])
        data=rows()
        data['2026-10-13'].update(units=data['2026-10-12']['units']*4,nav=2.5,units_change=None)
        self.assertIn('疑似分割',f.week(data,date(2026,10,12),date(2026,10,16))['reason'])

    def test_partial_week_and_missing_four_week(self):
        con=sqlite3.connect(':memory:');store.ensure_schema(con)
        for day,r in rows().items():
            if day>='2026-10-09': f.upsert(con,'fund_daily',('code','date'),r)
        items=[{'code':'TEST'}];f.attach(con,items,'2026-10-14')
        self.assertEqual(items[0]['flows']['net_amount'],.3)
        self.assertIsNone(items[0]['flows']['four_week_amount'])
        self.assertIsNone(items[0]['flows']['positive_weeks'])
        con.close()

    def test_snapshot_dates_and_issuer_only(self):
        con=sqlite3.connect(':memory:');store.ensure_schema(con)
        issuer={'TEST':{'source':'測試投信官網','source_url':'https://www.yuantaetfs.com/test','nav_date':'2026-10-16',
            'pcf_date':'2026-10-15','nav_official':11,'pcf_nav':10,'aum':10,'units':100_000_000,'currency':'TWD'}}
        f.save(con,issuer,{})
        self.assertEqual(con.execute('SELECT nav,units FROM fund_daily WHERE date="2026-10-16"').fetchone(),(11,None))
        self.assertEqual(con.execute('SELECT nav,units FROM fund_daily WHERE date="2026-10-15"').fetchone(),(10,100_000_000))
        items=[{'code':'OTHER','aum':777,'units':999,'aum_source':'交易所推算'}];f.attach(con,items,'2026-10-16')
        self.assertIsNone(items[0]['aum']);self.assertIsNone(items[0]['units']);con.close()

    def test_monthly_fallback_and_holdings_date(self):
        con=sqlite3.connect(':memory:');store.ensure_schema(con)
        monthly={'TEST':{'code':'TEST','month':'2026-09','currency':'TWD','aum':12,'nav':10,'units':120_000_000,
             'subscriptions':1,'redemptions':2,'beneficiaries':0,'source':sitca.SOURCE,'source_url':sitca.BASE+'etf_statement.aspx'}}
        f.save(con,{},monthly)
        f.upsert(con,'fund_daily',('code','date'),{**rows()['2026-09-18'],'date':'2026-08-31'})
        for day in ['2026-10-15','2026-10-19']:
            con.execute('INSERT INTO holding(code,date,asset_code,kind,asset_name,weight,source,source_url) VALUES(?,?,?,?,?,?,?,?)',
                        ('TEST',day,'2330','股票',day,50,'測試投信官網','https://www.yuantaetfs.com/test'))
        items=[{'code':'TEST'}];f.attach(con,items,'2026-10-16')
        self.assertEqual(items[0]['aum_date'],'2026-09-30')
        self.assertEqual(items[0]['beneficiaries'],0)
        self.assertEqual(items[0]['holdings_date'],'2026-10-15')
        self.assertEqual(items[0]['sitca_monthly']['net_amount'],-1)
        self.assertIsNone(items[0]['flows']['net_amount']);con.close()

    def test_schema_migration_and_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);con=store.open_db(root/'first.db',None)
            f.upsert(con,'fund_daily',('code','date'),rows()['2026-10-16']);con.commit();store.dump(con,root/'history');con.close()
            con=store.open_db(root/'second.db',root/'history')
            self.assertEqual(con.execute('SELECT units FROM fund_daily').fetchone()[0],120_000_000)
            self.assertIn('source_url',[r[1] for r in con.execute('PRAGMA table_info(holding)')]);con.close()

    def test_future_month_or_late_publication_is_not_available_yet(self):
        con=sqlite3.connect(':memory:');store.ensure_schema(con)
        source={'source':sitca.SOURCE,'source_url':sitca.BASE+'etf_statement.aspx'}
        f.save(con,{}, {'TEST':{'code':'TEST','month':'2026-10','aum':20,'units':200_000_000,**source}})
        items=[{'code':'TEST'}];f.attach(con,items,'2026-10-16')
        self.assertIsNone(items[0]['sitca_monthly'])
        f.save(con,{}, {'TEST':{'code':'TEST','month':'2026-09','aum':10,'units':100_000_000,'published_at':'2026-10-15',**source}})
        f.attach(con,items,'2026-10-14')
        self.assertIsNone(items[0]['sitca_monthly'])
        self.assertIsNone(items[0]['aum']);con.close()


class SitcaTest(unittest.TestCase):
    def page(self):
        headers=['證券代號','計價幣別','基金規模(台幣)','單位淨值(台幣)','受益權單位數','本月申購總金額(台幣)','本月買回總金額(台幣)','總受益人數']
        cells=['0050','TWD','1,000,000,000','10','100,000,000','0','100,000,000','1000']
        return '<span id="x_lblMRdate">2026.10.15</span><table><tr>'+''.join('<th>'+h+'</th>' for h in headers)+'</tr><tr>'+''.join('<td>'+c+'</td>' for c in cells)+'</tr></table>'

    def test_units_period_zero_and_publication_date(self):
        r=sitca.parse(self.page(),'202609')['0050']
        self.assertEqual((r['aum'],r['subscriptions'],r['redemptions']),(10,0,1))
        self.assertEqual((r['month'],r['published_at']),('2026-09','2026-10-15'))
        self.assertIsNone(sitca.number('—'));self.assertIsNone(sitca.number('nan'))

    def test_schema_changes_and_duplicate_codes_fail_closed(self):
        with self.assertRaises(ValueError):sitca.parse(self.page().replace('基金規模(台幣)','基金規模(千元)'),'202609')
        row=self.page()[self.page().index('<tr><td>'):self.page().index('</table>')]
        with self.assertRaises(ValueError):sitca.parse(self.page().replace('</table>',row+'</table>'),'202609')
        self.assertIsNone(sitca.parse(self.page().replace('TWD','USD'),'202609')['0050']['nav'])

    def test_latest_month_uses_published_options(self):
        self.assertEqual(sitca.latest_month('<select id="x_ddlYM"><option value="202608">8</option><option value="202609">9</option></select>'),'202609')
        self.assertIsNone(sitca.latest_month('<html>網站維護</html>'))


if __name__=='__main__':unittest.main()
