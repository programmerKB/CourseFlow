"""Curriculum paths from SRS §12, maintained separately from live offerings."""
from dataclasses import dataclass
from .catalog_data import NTU_NE, NTNU_EE, NTUST_ET


ROADMAP_VERSION = '3.2'


@dataclass(frozen=True)
class Track:
    title: str
    aliases: tuple[str, ...]
    foundation_goals: tuple[str, ...]
    foundation: tuple[str, ...]
    specialization: tuple[str, ...]
    project: str
    deliverable: str
    metrics: tuple[str, ...]


@dataclass(frozen=True)
class Reference:
    title: str
    department: str
    source_url: str


REFERENCES = {
    'rtos': Reference('嵌入式即時作業系統', 'NTUST-ET', NTUST_ET),
    'fpga': Reference('FPGA 系統設計實務', 'NTUST-ET', NTUST_ET),
    'microwave': Reference('微波工程', 'NTNU-EE', NTNU_EE),
    'ic_engineering': Reference('積體電路工程', 'NTU-ET', NTU_NE),
    'thin_film': Reference('薄膜工程專題', 'NTU-ET', NTU_NE),
    'integrated_optics': Reference('積體光學', 'NTU-ET', NTU_NE),
    'optoelectronics': Reference('光電半導體技術', 'NTU-ET', NTU_NE),
    'lasers': Reference('半導體雷射原理', 'NTU-ET', NTU_NE),
    'robotics': Reference('先進機器人感測與控制', 'NTU-EE', 'https://web.ee.ntu.edu.tw/course_detail.php?CA_ID=6103'),
}

TRACKS = {
    'embedded_systems': Track(
        '嵌入式系統', ('MCU', 'RTOS', 'STM32', 'embedded systems'),
        ('掌握 C／C++、數位邏輯與計算機結構', '理解作業系統、中斷與周邊介面'),
        ('CS101', 'EE301', 'CS302'), ('NTNU-EE-006', 'NTU-EE-004', 'rtos'),
        '在開發板實作感測器採樣、即時排程與資料傳輸。',
        '韌體、接線說明與量測腳本。', ('事件延遲', '採樣抖動', '記憶體用量'),
    ),
    'memory_systems': Track(
        '計算機結構與記憶體系統', ('計算機結構', '記憶體系統', '快取', '記憶體階層', 'NVM 架構', 'memory systems'),
        ('理解計算機結構、數位邏輯和作業系統', '用 C／C++ 建立可重跑的記憶體存取實驗'),
        ('EE301', 'CS302'), ('NTUST-ET-001', 'NTNU-CS-002', 'NTUST-CS-002', 'NTUST-ET-007'),
        '比較快取替換、記憶體管理或 NVM 控制器策略。',
        '模擬器設定、工作負載與結果表。', ('快取命中率', '平均存取延遲', '吞吐量', '寫入次數'),
    ),
    'edge_ai': Track(
        '邊緣 AI 與模型部署', ('邊緣 AI', 'Edge AI', 'TinyML', '邊緣推論'),
        ('掌握機器學習、深度學習及模型評估', '補足嵌入式與訊號處理基礎'),
        ('NTNU-CS-003', 'EE302', 'NTNU-EE-006'), ('NTUST-ET-005', 'NTUST-ET-004', 'NTU-CS-004'),
        '在 Jetson 或 MCU 比較同一模型的原始與量化版本。',
        '模型、部署步驟與量測結果。', ('準確率', '推論延遲', '記憶體', '能耗'),
    ),
    'llm': Track(
        '大型語言模型', ('大語言模型', 'LLM', 'large language model', 'large language models'),
        ('掌握 Python、線性代數與機率統計', '練習機器學習、深度學習和模型評估'),
        ('CS101', 'NTNU-CS-003'), ('NTU-CS-001', 'NTU-CS-004', 'NTU-TE-001', 'DEMO-503'),
        '用固定題集比較直接回答、檢索增強或不同檢索策略。',
        '測試題集、實驗程式及輸出紀錄。', ('回答正確率', '引文與證據一致性', '延遲', 'Token 用量'),
    ),
    'ai_agent': Track(
        'AI Agent 與工具呼叫', ('AI Agent', 'AI 代理人', 'Agentic AI', 'AI 智慧代理', '工具呼叫'),
        ('練習 Python、API 設計、資料庫與軟體測試', '理解語言模型如何呼叫工具和管理狀態'),
        ('CS101', 'NTUST-ET-002'), ('NTU-CS-001', 'NTU-CS-002', 'NTU-CS-005'),
        '用 CourseFlow 評估工具選擇、查詢與使用者確認流程。',
        '測試題集、流程執行紀錄與可重跑的實驗腳本。', ('工具選對率', '端到端成功率', '重送一致性', '操作錯誤率'),
    ),
    'ml_vision': Track(
        '機器學習與電腦視覺', ('機器學習', '電腦視覺', '影像辨識', 'ML', 'CV'),
        ('掌握線性代數、機率統計與 Python', '理解影像處理、資料切分和模型驗證'),
        ('CS101', 'NTNU-CS-003', 'NTUST-CS-001'), ('NTNU-CS-004', 'NTUST-ET-004', 'NTU-CS-004'),
        '訓練瑕疵偵測或影像辨識模型，分析光照和場景變化的影響。',
        '固定的資料切分、基準模型及評估程式。', ('F1 或 mAP', '推論時間', '各場景錯誤率'),
    ),
    'data_systems': Track(
        '資料庫與分散式系統', ('資料庫', '分散式系統', 'SQL', 'DBMS', '資料工程'),
        ('理解資料結構、作業系統及網路', '練習關聯模型與 SQL'),
        ('CS201', 'CS302', 'CS303'), ('NTUST-ET-002', 'NTNU-CS-005', 'NTU-CS-002'),
        '用 CourseFlow 比較資料庫索引及交易重送，測量併發工作負載。',
        '基準負載、SQL 執行計畫與資料一致性驗證。', ('p95 查詢延遲', '吞吐量', '一致性錯誤數'),
    ),
    'digital_ic': Track(
        '數位 IC 與 VLSI', ('數位 IC', 'RTL', 'FPGA', 'FPGA 設計', 'VLSI', '數位晶片'),
        ('補足數位邏輯、計算機結構與電子學', '練習 Verilog 或 SystemVerilog 模擬'),
        ('EE301', 'EE201'), ('NTU-ET-004', 'NTU-ET-002', 'fpga'),
        '用 RTL 實作小型運算模組並比較不同實作策略。',
        'RTL、測試平台、綜合及時序報告。', ('資源用量', '可達時脈', '估計功耗'),
    ),
    'analog_ic': Track(
        '類比與混合訊號 IC', ('類比 IC', '混合訊號 IC', 'Analog IC', 'Mixed-Signal', 'ADC', 'DAC'),
        ('理解電路、電子學與 MOS 元件', '練習小訊號模型、回授與雜訊分析'),
        ('EE101', 'EE201'), ('NTU-ET-001', 'NTNU-EE-004', 'NTUST-ET-006'),
        '模擬放大器、資料轉換器或電源電路，量測製程變異下的表現。',
        '電路圖、模擬設定及變異分析。', ('增益', '頻寬', '雜訊', '耗電或轉換誤差'),
    ),
    'semiconductor_process': Track(
        '半導體元件與製程', ('半導體製程', '製程整合', '元件物理', 'TCAD', 'NVM 製程'),
        ('補足普通物理、固態物理與半導體物理', '理解製程和元件特性的關係'),
        ('EE201',), ('NTU-ET-005', 'ic_engineering', 'thin_film'),
        '用公開資料或 TCAD 比較元件尺寸和製程變異對電性影響。',
        '模擬設定、參數掃描與可重現的比較報告。', ('臨界電壓', '漏電', '電流特性'),
    ),
    'photonics': Track(
        '光電與光子元件', ('積體光學', '光子元件', 'Photonic IC', '光電'),
        ('複習電磁學、波動光學與半導體物理', '練習光學數值方法'),
        ('NTU-ET-005',), ('integrated_optics', 'optoelectronics', 'lasers'),
        '用數值模擬比較兩種波導或耦合器幾何。',
        '場分布、模型設定與參數掃描。', ('插入損耗', '耦合效率', '工作頻寬'),
    ),
    'communications': Track(
        '通訊系統', ('數位通訊', '無線通訊', 'OFDM', 'MIMO', '通信系統'),
        ('掌握訊號與系統、線性代數及通訊原理', '理解機率和隨機程序'),
        ('EE302',), ('NTNU-EE-002', 'NTUST-EE-004'),
        '在模擬鏈路比較調變、通道估測或等化策略。',
        '通道設定、模擬程式與 BER 對 SNR 曲線。', ('位元錯誤率', '頻譜效率'),
    ),
    'rf_microwave': Track(
        '射頻與微波電路', ('RF', '射頻', '微波', 'RFIC', 'microwave'),
        ('掌握電路、電磁學與傳輸線', '練習 Smith 圖和 S 參數'),
        ('EE101',), ('microwave', 'NTNU-EE-005'),
        '模擬阻抗匹配網路、低雜訊放大器或濾波器。',
        '模擬電路、頻率響應與設計報告。', ('反射係數', '增益', '頻寬', '雜訊指數'),
    ),
    'signal_sensing': Track(
        '訊號處理與智慧感測', ('訊號處理', '信號處理', 'DSP', '振動分析', '感測器訊號'),
        ('練習取樣、傅立葉分析與線性代數', '理解訊號與系統及機率統計'),
        ('EE302',), ('NTNU-EE-001', 'NTU-ET-003'),
        '用振動或超音波資料比較濾波、頻譜特徵與異常偵測。',
        '分析程式、測試資料及固定量測流程。', ('訊雜比', '偵測召回率', '運算量'),
    ),
    'control_robotics': Track(
        '控制與機器人', ('控制系統', '狀態空間', '機器人控制', '機器人', '回授控制'),
        ('補足線性代數、微分方程與訊號基礎', '練習回授控制和穩定性分析'),
        ('EE302',), ('NTNU-EE-003', 'NTU-EE-002', 'robotics'),
        '在模擬或低功率實驗平台比較 PID 和狀態回授。',
        '模型及同一目標軌跡的比較報告。', ('追蹤誤差', '穩定時間', '抗干擾表現'),
    ),
    'power_electronics': Track(
        '電力電子與能源系統', ('電力電子', '電力系統', '電力轉換', '能源系統'),
        ('複習電路、電子學及回授控制', '理解電力電路和負載模型'),
        ('EE101', 'EE201'), ('NTUST-EE-002', 'NTUST-EE-001', 'NTU-EE-003'),
        '用模擬比較不同轉換器控制或電力系統穩定性。',
        '可重跑的模型、負載情境與波形。', ('穩態誤差', '暫態響應', '損耗'),
    ),
}
