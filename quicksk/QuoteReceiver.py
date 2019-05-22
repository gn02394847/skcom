import os
import json
import math
import time
import shutil
import signal
import os.path
from datetime import datetime, timedelta

import pythoncom
import comtypes.client
import comtypes.gen.SKCOMLib as sk

class QuoteReceiver():
    """
    群益 API 報價接收器

    參考文件 v2.13.16:
      * 4-1 SKCenterLib (p.18)
      * 4-4 SKQuoteLib (p.93)
    """

    def __init__(self, gui_mode=False):
        # 狀態屬性
        self.done = False
        self.ready = False
        self.stopping = False

        # 接收器設定屬性
        self.gui_mode = gui_mode
        self.log_path = os.path.expanduser('~\\.skcom\\logs')
        self.dst_conf = os.path.expanduser('~\\.skcom\\quicksk.json')

        # Ticks 處理用屬性
        self.ticks_hook = None
        self.ticks_total = {}

        # 日 K 處理用屬性
        self.kline_hook = None
        self.stock_name = {}
        self.daily_kline = {}

        valid_config = False
        tpl_conf = os.path.dirname(os.path.realpath(__file__)) + '\\conf\\quicksk.json'

        if not os.path.isfile(self.dst_conf):
            # 產生 log 目錄
            if not os.path.isdir(self.log_path):
                os.makedirs(self.log_path)
            # 複製設定檔範本
            shutil.copy(tpl_conf, self.dst_conf)
        else:
            # 載入設定檔
            with open(self.dst_conf, 'r') as cfgfile:
                self.config = json.load(cfgfile)
                if self.config['account'] != 'A123456789':
                    valid_config = True

        if not valid_config:
            self.prompt()

    def prompt(self):
        # 提示
        print('請開啟設定檔，將帳號密碼改為您的證券帳號')
        print('設定檔路徑: ' + self.dst_conf)
        exit(0)

    def ctrl_c(self, sig, frm):
        if not self.done and not self.stopping:
            print('偵測到 Ctrl+C, 結束監聽')
            self.stop()

    def set_kline_hook(self, hook, days_limit=20):
        self.kline_days_limit = days_limit
        self.kline_hook = hook

    def set_ticks_hook(self, hook, include_history=False):
        self.ticks_hook = hook
        self.ticks_include_history = include_history

    def start(self):
        """
        開始接收報價
        """
        if self.ticks_hook is None and self.kline_hook is None:
            print('沒有設定監聽項目')
            return

        try:
            signal.signal(signal.SIGINT, self.ctrl_c)

            # 登入
            self.skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            self.skC.SKCenterLib_SetLogPath(self.log_path)
            nCode = self.skC.SKCenterLib_Login(self.config['account'], self.config['password'])
            if nCode != 0:
                # 沒插網路線會回傳 1001, 不會觸發 OnConnection
                self.handleSkError('Login()', nCode)
                return
            print('登入成功')

            # 建立報價連線
            # 注意: comtypes.client.GetEvents() 有雷
            # * 一定要收回傳值, 即使這個回傳值沒用到, 如果不收回傳值會導致事件收不到
            # * 指定給 self.skH 會在程式結束時產生例外
            # * 指定給 global skH 會在程式結束時產生例外
            self.skQ = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
            skH = comtypes.client.GetEvents(self.skQ, self)
            nCode = self.skQ.SKQuoteLib_EnterMonitor()
            if nCode != 0:
                # 這裡拔網路線會得到 3022, 查表沒有對應訊息
                self.handleSkError('EnterMonitor()', nCode)
                return
            print('連線成功')

            # 等待連線就緒
            while not self.ready and not self.done:
                time.sleep(1)
                if not self.gui_mode:
                    pythoncom.PumpWaitingMessages()

            if self.done: return
            print('連線就緒')

            # 這時候拔網路線疑似會陷入無窮迴圈
            # time.sleep(3)

            # 接收 Ticks
            if self.ticks_hook is not None:
                if len(self.config['products']) > 50:
                    # 發生這個問題不阻斷使用, 讓其他功能維持正常運作
                    print('Ticks 最多只能監聽 50 檔')
                else:
                    for stock_no in self.config['products']:
                        # 參考文件: 4-4-6 (p.97)
                        # 1. 這裡的回傳值是個 list [pageNo, nCode], 與官方文件不符
                        # 2. 參數 psPageNo 在官方文件上表示一個 pn 只能對應一檔股票, 但實測發現可以一對多,
                        #    因為這樣, 實際上可能可以突破只能聽 50 檔的限制, 不過暫時先照文件友善使用 API
                        # 3. 參數 psPageNo 指定 -1 會自動分配 page, page 介於 0-49, 與 stock page 不同
                        # 4. 參數 psPageNo 指定 50 會取消報價
                        (pageNo, nCode) = self.skQ.SKQuoteLib_RequestTicks(-1, stock_no)
                        # print('tick page=%d' % pageNo)
                        if nCode != 0:
                            self.handleSkError('RequestTicks()', nCode)
                    # print('Ticks 請求完成')

            # 接收日 K
            if self.kline_hook is not None:
                # 取樣截止日
                # 15:00 以前取樣到昨日
                # 15:00 以後取樣到當日
                n = datetime.today()
                human_min = n.hour * 100 + n.minute
                day_offset = 0
                if human_min < 1500:
                    day_offset = 1
                self.end_date = (datetime.today() - timedelta(days=day_offset)).strftime('%Y-%m-%d')

                # 載入股票代碼/名稱對應
                for stock_no in self.config['products']:
                    # 參考文件: 4-4-5 (p.97)
                    # 1. 參數 pSKStock 可以省略
                    # 2. 回傳值是 list [SKSTOCKS*, nCode], 與官方文件不符
                    (pStock, nCode) = self.skQ.SKQuoteLib_GetStockByNo(stock_no)
                    if nCode != 0:
                        self.handleSkError('GetStockByNo()', nCode)
                        return
                    self.daily_kline[pStock.bstrStockNo] = {
                        'id': pStock.bstrStockNo,
                        'name': pStock.bstrStockName,
                        'quotes': []
                    }
                # print('股票名稱載入完成')

                # 請求日 K
                for stock_no in self.config['products']:
                    # 參考文件: 4-4-9 (p.99), 4-4-21 (p.105)
                    # 1. 使用方式與文件相符
                    # 2. 台股日 K 使用全盤與 AM 盤效果相同
                    nCode = self.skQ.SKQuoteLib_RequestKLine(stock_no, 4, 1)
                    # nCode = self.skQ.SKQuoteLib_RequestKLineAM(stock_no, 4, 1, 1)
                    if nCode != 0:
                        self.handleSkError('RequestKLine()', nCode)
                # print('日 K 請求完成')

            # 命令模式下等待 Ctrl+C
            if not self.gui_mode:
                while not self.done:
                    pythoncom.PumpWaitingMessages()
                    time.sleep(0.5)

            print('監聽結束')
        except Exception as ex:
            print('init() 發生不預期狀況', flush=True)
            print(ex)

    def stop(self):
        """
        停止接收報價
        """
        if self.skQ is not None:
            self.stopping = True
            nCode = self.skQ.SKQuoteLib_LeaveMonitor()
            if nCode != 0:
                self.handleSkError('EnterMonitor', nCode)
        else:
            self.done = True

    def handleSkError(self, action, nCode):
        """
        處理群益 API 元件錯誤
        """
        # 參考文件: 4-1-3 (p.19)
        skmsg = self.skC.SKCenterLib_GetReturnCodeMessage(nCode)
        msg = '執行動作 [%s] 時發生錯誤, 詳細原因: %s' % (action, skmsg)
        print(msg)

    def handleTicks(self, id, name, time, bid, ask, close, qty, vol):
        """
        處理當天回補 ticks 或即時 ticks
        """
        entry = {
            'id': id,
            'name': name,
            'time': time,
            'bid': bid,
            'ask': ask,
            'close': close,
            'qty': qty,
            'vol': vol
        }
        self.ticks_hook(entry)

    def OnConnection(self, nKind, nCode):
        """
        接收連線狀態變更 4-4-a (p.107)
        """
        if nCode != 0:
            # 這裡的 nCode 沒有對應的文字訊息
            action = '狀態變更 %d' % nKind
            self.handleSkError(action, nCode)

        # 參考文件: 6. 代碼定義表 (p.170)
        # 3001 已連線
        # 3002 正常斷線
        # 3003 已就緒
        # 3021 異常斷線
        if nKind == 3003:
            self.ready = True
        if nKind == 3002 or nKind == 3021:
            self.done = True
            print('斷線')

    def OnNotifyHistoryTicks(self, sMarketNo, sStockidx, nPtr, \
                      nDate, nTimehms, nTimemillis, \
                      nBid, nAsk, nClose, nQty, nSimulate):
        """
        接收當天回補撮合 Ticks 4-4-c (p.108)
        """
        # 忽略試撮回報
        # 13:30:00 的最後一筆撮合, 即使收盤後也是透過一般 Ticks 觸發, 不會出現在回補資料中
        # [2330 台積電] 時間:13:24:59.463 買:238.00 賣:238.50 成:238.50 單量:43 總量:31348
        if nTimehms < 90000 or nTimehms >= 132500:
            return

        # 參考文件: 4-4-4 (p.96)
        # 1. pSKStock 參數可忽略
        # 2. 回傳值是 list [SKSTOCKS*, nCode], 與官方文件不符
        # 3. 如果沒有 RequestStocks(), 這裡得到的總量恆為 0
        (pStock, nCode) = self.skQ.SKQuoteLib_GetStockByIndex(sMarketNo, sStockidx)
        if nCode != 0:
            self.handleSkError('GetStockByIndex()', nCode)
            return

        # 累加總量
        # 總量採用歷史與即時撮合累加最理想, 如果用 pStock.nTQty 會讓回補撮合的總量顯示錯誤
        if pStock.bstrStockNo not in self.ticks_total:
            self.ticks_total[pStock.bstrStockNo] = nQty
        else:
            self.ticks_total[pStock.bstrStockNo] += nQty

        if self.ticks_include_history:
            # 時間字串化
            s = nTimehms % 100
            nTimehms /= 100
            m = nTimehms % 100
            nTimehms /= 100
            h = nTimehms
            timestr = '%02d:%02d:%02d.%03d' % (h, m, s, nTimemillis//1000)

            # 格式轉換
            ppow = math.pow(10, pStock.sDecimal)
            self.handleTicks(
                pStock.bstrStockNo,
                pStock.bstrStockName,
                timestr,
                nBid / ppow,
                nAsk / ppow,
                nClose / ppow,
                nQty,
                self.ticks_total[pStock.bstrStockNo] # pStock.nTQty
            )

    def OnNotifyTicks(self, sMarketNo, sStockidx, nPtr, \
                      nDate, nTimehms, nTimemillis, \
                      nBid, nAsk, nClose, nQty, nSimulate):
        """
        接收即時撮合 4-4-d (p.109)
        """
        # 忽略試撮回報
        # 盤中最後一筆與零股交易, 即使收盤也不會觸發歷史 Ticks, 這兩筆會在這裡觸發
        # [2330 台積電] 時間:13:24:59.463 買:238.00 賣:238.50 成:238.50 單量:43 總量:31348
        # [2330 台積電] 時間:13:30:00.000 買:238.00 賣:238.50 成:238.00 單量:3221 總量:34569
        # [2330 台積電] 時間:14:30:00.000 買:0.00 賣:0.00 成:238.00 單量:18 總量:34587
        if nTimehms < 90000 or (nTimehms >= 132500 and nTimehms < 133000):
            return

        # 參考文件: 4-4-4 (p.96)
        # 1. pSKStock 參數可忽略
        # 2. 回傳值是 list [SKSTOCKS*, nCode], 與官方文件不符
        # 3. 如果沒有 RequestStocks(), 這裡得到的總量 pStock.nTQty 恆為 0
        (pStock, nCode) = self.skQ.SKQuoteLib_GetStockByIndex(sMarketNo, sStockidx)
        if nCode != 0:
            self.handleSkError('GetStockByIndex()', nCode)
            return

        # 累加總量
        if pStock.bstrStockNo not in self.ticks_total:
            self.ticks_total[pStock.bstrStockNo] = nQty
        else:
            self.ticks_total[pStock.bstrStockNo] += nQty

        # 時間字串化
        s = nTimehms % 100
        nTimehms /= 100
        m = nTimehms % 100
        nTimehms /= 100
        h = nTimehms
        timestr = '%02d:%02d:%02d.%03d' % (h, m, s, nTimemillis//1000)

        # 格式轉換
        ppow = math.pow(10, pStock.sDecimal)
        self.handleTicks(
            pStock.bstrStockNo,
            pStock.bstrStockName,
            timestr,
            nBid / ppow,
            nAsk / ppow,
            nClose / ppow,
            nQty,
            self.ticks_total[pStock.bstrStockNo]
        )

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        """
        接收 K 線資料 (文件 4-4-f p.112)
        """

        # 新版 K 線資料格式
        # 日期        開           高          低          收          量
        # 2019/05/21, 233.500000, 236.000000, 232.500000, 234.000000, 79971
        cols = bstrData.split(', ')
        this_date = cols[0].replace('/', '-')

        if self.daily_kline[bstrStockNo] is not None:
            # 寫入緩衝區與交易日數限制處理
            quote = {
                'date': this_date,
                'open': float(cols[1]),
                'high': float(cols[2]),
                'low': float(cols[3]),
                'close': float(cols[4]),
                'volume': int(cols[5])
            }
            buffer = self.daily_kline[bstrStockNo]['quotes']
            buffer.append(quote)
            if self.kline_days_limit > 0 and len(buffer) > self.kline_days_limit:
                buffer.pop(0)

            # 取得最後一筆後觸發 hook, 並且清除緩衝區
            if this_date == self.end_date:
                self.kline_hook(self.daily_kline[bstrStockNo])
                self.daily_kline[bstrStockNo] = None

        # 除錯用, 確認當日資料產生時機後就刪除
        # 14:00, kline 會收到昨天
        # 14:30, 待確認
        # 15:00, kline 會收到當天
        if this_date > self.end_date:
            print('當日資料已產生', this_date)
