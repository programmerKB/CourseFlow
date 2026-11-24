"""Versioned course references for the simulated three-university catalog."""
from unicodedata import normalize
from uuid import NAMESPACE_URL, uuid5


SCHOOLS = {
    'NTU': ('國立臺灣大學', '臺大', '台大', '臺灣大學', '台灣大學'),
    'NTNU': ('國立臺灣師範大學', '臺師大', '台師大', '師大', '臺灣師範大學', '台灣師範大學'),
    'NTUST': ('國立臺灣科技大學', '臺科大', '台科大', '臺灣科技大學', '台灣科技大學'),
}
NTUST_ID = str(uuid5(NAMESPACE_URL, 'ntust-course-demo/school/NTUST'))

# School, department part of course code, database department code and display name.
DEPARTMENTS = {
    'NTU-EE': ('EE', '電機工程學研究所', ('電機系', '電機所')),
    'NTU-ET': ('ET', '電子工程學研究所', ('電子系', '電子所')),
    'NTU-CS': ('CS', '資訊工程學研究所', ('資訊系', '資工系', '資工所')),
    'NTU-TE': ('TE', '電信工程學研究所', ('電信系', '電信所')),
    'NTNU-EE': ('EE', '電機工程學系碩士班', ('電機系', '電機所')),
    'NTNU-CS': ('CS', '資訊工程學系碩士班', ('資訊系', '資工系', '資工所')),
    'NTUST-EE': ('ELEC', '電機工程系', ('電機系', '電機所', '電機工程系研究所')),
    'NTUST-ET': ('EE', '電子工程系', ('電子系', '電子所', '電子工程系研究所')),
    'NTUST-CS': ('CS', '資訊工程系', ('資訊系', '資工系', '資工所', '資訊工程系研究所')),
}

NTU_EE = 'https://coursemap.aca.ntu.edu.tw/course_map_all/class.php?code=921M'
NTU_IC = 'https://giee.ntu.edu.tw/tw/courses/ics/'
NTU_NE = 'https://giee.ntu.edu.tw/tw/courses/ne/'
NTNU_EE = 'https://course.ntnu.edu.tw/coursemapdata/couHM75.pdf'
NTNU_CS = 'https://www.csie.ntnu.edu.tw/index.php/course_ms/course/'
NTUST_EE = 'https://www.ee.ntust.edu.tw/var/file/27/1027/img/3406/ElectiveCourses_Graduate.pdf'
NTUST_ET = 'https://ece.ntust.edu.tw/p/412-1017-90.php?Lang=zh-tw'
NTUST_AI = 'https://ai-center.ntust.edu.tw/courses/schools?school=0022&term=1151'


def ntu_syllabus(department, course, term, serial):
    return (f'https://nol.ntu.edu.tw/nol/coursesearch/print_table.php?class=&course_id={course}'
            f'&dpt_code={department}&lang=CH&semester={term}&ser_no={serial}')


# Each row defines a title, primary and related search tags, and the source
# supplied in the requirements. Class times and capacity are synthetic.
COURSE_GROUPS = {
    'NTU-EE': (
        ('適應控制系統', ('control_robotics',), 'https://course.ntu.edu.tw/courses/114-2/26729'),
        ('最佳控制', ('control_robotics',), 'https://course.ntu.edu.tw/courses/114-2/12636'),
        ('電力系統控制與穩定度', ('power_electronics', 'control_robotics'), 'https://course.ntu.edu.tw/courses/113-1/56229'),
        ('嵌入式系統與軟體工程', ('embedded_systems', 'edge_ai'), NTU_EE),
    ),
    'NTU-ET': (
        ('類比積體電路', ('analog_ic',), NTU_IC),
        ('電腦輔助積體電路系統設計', ('digital_ic',), NTU_IC),
        ('數位訊號處理架構設計', ('signal_sensing', 'digital_ic'), NTU_IC),
        ('高等數位系統設計', ('digital_ic', 'memory_systems'), NTU_IC),
        ('半導體元件物理', ('semiconductor_process', 'photonics'), NTU_NE),
    ),
    'NTU-CS': (
        ('自然語言處理', ('nlp', 'llm', 'ai_agent'), ntu_syllabus('9220', '922+U0670', '114-2', '10508')),
        ('分散式系統', ('data_systems', 'ai_agent'), ntu_syllabus('9220', '922+U1580', '114-2', '48414')),
        ('高等作業系統', ('memory_systems', 'data_systems'), ntu_syllabus('9220', '922+M1360', '110-2', '80197')),
        ('深度學習之應用', ('deep_learning', 'ml_vision', 'llm', 'edge_ai'), ntu_syllabus('9220', '922+U4340', '114-1', '26131')),
        ('資料庫系統設計', ('data_systems', 'ai_agent'), ntu_syllabus('9220', '922+M1440', '96-1', '16911')),
    ),
    'NTU-TE': (
        ('大型語言與語音模型技術', ('llm', 'nlp', 'signal_sensing'), ntu_syllabus('9420', '921+U3580', '115-1', '12317')),
    ),
    'NTNU-EE': (
        ('高等數位信號處理', ('signal_sensing',), NTNU_EE),
        ('數位通訊系統', ('communications',), NTNU_EE),
        ('控制系統設計', ('control_robotics',), NTNU_EE),
        ('類比積體電路設計', ('analog_ic',), NTNU_EE),
        ('射頻與微波積體電路', ('rf_microwave', 'analog_ic'), NTNU_EE),
        ('嵌入式系統', ('embedded_systems', 'edge_ai'), NTNU_EE),
    ),
    'NTNU-CS': (
        ('分散式處理系統', ('data_systems', 'ai_agent'), NTNU_CS),
        ('高等計算機結構', ('memory_systems', 'embedded_systems'), NTNU_CS),
        ('機器學習', ('ml_vision', 'deep_learning', 'edge_ai'), NTNU_CS),
        ('電腦視覺', ('ml_vision', 'edge_ai'), NTNU_CS),
        ('高等資料庫系統', ('data_systems', 'ai_agent'), NTNU_CS),
    ),
    'NTUST-EE': (
        ('電力系統分析與實務', ('power_electronics',), NTUST_EE),
        ('電力電子建模與分析', ('power_electronics', 'control_robotics'), NTUST_EE),
        ('數位控制系統', ('control_robotics',), NTUST_EE),
        ('先進行動通訊系統', ('communications', 'rf_microwave'), NTUST_EE),
    ),
    'NTUST-ET': (
        ('高等計算機結構', ('memory_systems', 'embedded_systems', 'digital_ic'), NTUST_ET),
        ('資料庫設計', ('data_systems', 'ai_agent'), NTUST_ET),
        ('計算機演算法', ('data_systems', 'ml_vision'), NTUST_ET),
        ('智慧型視訊監控系統', ('ml_vision', 'edge_ai', 'signal_sensing'), NTUST_ET),
        ('嵌入式深度神經網路處理', ('edge_ai', 'embedded_systems', 'deep_learning'), NTUST_AI),
        ('類比積體電路設計與佈局', ('analog_ic', 'semiconductor_process'), NTUST_ET),
        ('儲存系統架構與應用', ('memory_systems', 'data_systems'), NTUST_ET),
    ),
    'NTUST-CS': (
        ('機器學習', ('ml_vision', 'deep_learning', 'edge_ai'), NTUST_AI),
        ('高等計算機結構', ('memory_systems', 'embedded_systems'), 'https://www.csie.ntust.edu.tw/var/file/38/1038/img/2176/344112436.pdf'),
        ('強化學習於人機互動系統開發', ('ml_vision', 'ai_agent', 'control_robotics'), NTUST_AI),
    ),
}
COURSE_ALIASES = {
    'NTU-CS-001': ('NLP', '自然語言'),
    'NTU-TE-001': ('語音模型技術', '大型語言和語音模型技術'),
    'NTNU-EE-001': ('高等數位訊號處理',),
    'NTNU-EE-002': ('數位通信系統',),
    'NTNU-EE-005': ('RFIC', '射頻與微波IC'),
    'NTNU-EE-006': ('嵌入式',),
    'NTNU-CS-002': ('高等計算機架構',),
    'NTUST-ET-001': ('高等計算機架構', '高等計組'),
    'NTUST-ET-002': ('DB設計', '資料庫系統設計'),
    'NTUST-ET-005': ('嵌入式DNN處理', '邊緣AI'),
    'NTUST-ET-006': ('類比積體電路設計與布局',),
    'NTUST-ET-007': ('儲存系統架構',),
    'NTUST-CS-002': ('高等計算機架構',),
}
# English names let the English assistant and the web search resolve courses whose titles are
# in Chinese (SRS §5.2 course_aliases). Names shared by two schools stay ambiguous on purpose,
# so the assistant asks the student to choose a school and section.
ENGLISH_ALIASES = {
    'CS101': ('Programming', 'Introduction to Programming', 'Intro to Programming'),
    'CS201': ('Data Structures', 'Data Structure'),
    'CS301': ('Database Design', 'Database Systems'),
    'CS302': ('Operating Systems', 'Operating System'),
    'CS303': ('Computer Networks', 'Computer Networking'),
    'CS304': ('Software Engineering',),
    'CS399': ('AI Project', 'Artificial Intelligence Project'),
    'EE101': ('Circuits', 'Circuit Theory', 'Electric Circuits'),
    'EE201': ('Electronics',),
    'EE301': ('Digital Logic Design', 'Digital Logic'),
    'EE302': ('Signals and Systems',),
    'DEMO-501': ('Large Language Model Project', 'LLM Project'),
    'DEMO-502': ('Natural Language Processing',),
    'DEMO-503': ('Information Retrieval and RAG',),
    'DEMO-504': ('AI Systems and Model Deployment',),
    'DEMO-505': ('Vector Databases and Semantic Search',),
    'NTU-EE-001': ('Adaptive Control Systems',),
    'NTU-EE-002': ('Optimal Control',),
    'NTU-EE-003': ('Power System Control and Stability',),
    'NTU-EE-004': ('Embedded Systems and Software Engineering',),
    'NTU-ET-001': ('Analog Integrated Circuits',),
    'NTU-ET-002': ('Computer-Aided Design of Integrated Circuit Systems',),
    'NTU-ET-003': ('DSP Architecture Design', 'Digital Signal Processing Architecture Design'),
    'NTU-ET-004': ('Advanced Digital System Design',),
    'NTU-ET-005': ('Semiconductor Device Physics',),
    'NTU-CS-001': ('Natural Language Processing',),
    'NTU-CS-002': ('Distributed Systems',),
    'NTU-CS-003': ('Advanced Operating Systems',),
    'NTU-CS-004': ('Applied Deep Learning', 'Applications of Deep Learning'),
    'NTU-CS-005': ('Database System Design',),
    'NTU-TE-001': ('Large Language and Speech Model Technology',),
    'NTNU-EE-001': ('Advanced Digital Signal Processing',),
    'NTNU-EE-002': ('Digital Communication Systems',),
    'NTNU-EE-003': ('Control System Design',),
    'NTNU-EE-004': ('Analog Integrated Circuit Design',),
    'NTNU-EE-005': ('RF and Microwave Integrated Circuits',),
    'NTNU-EE-006': ('Embedded Systems',),
    'NTNU-CS-001': ('Distributed Processing Systems',),
    'NTNU-CS-002': ('Advanced Computer Architecture',),
    'NTNU-CS-003': ('Machine Learning',),
    'NTNU-CS-004': ('Computer Vision',),
    'NTNU-CS-005': ('Advanced Database Systems',),
    'NTUST-EE-001': ('Power System Analysis and Practice',),
    'NTUST-EE-002': ('Power Electronics Modeling and Analysis',),
    'NTUST-EE-003': ('Digital Control Systems',),
    'NTUST-EE-004': ('Advanced Mobile Communication Systems',),
    'NTUST-ET-001': ('Advanced Computer Architecture',),
    'NTUST-ET-002': ('Database Design',),
    'NTUST-ET-003': ('Computer Algorithms',),
    'NTUST-ET-004': ('Intelligent Video Surveillance Systems',),
    'NTUST-ET-005': ('Embedded Deep Neural Network Processing',),
    'NTUST-ET-006': ('Analog IC Design and Layout',),
    'NTUST-ET-007': ('Storage System Architecture and Applications',),
    'NTUST-CS-001': ('Machine Learning',),
    'NTUST-CS-002': ('Advanced Computer Architecture',),
    'NTUST-CS-003': ('Reinforcement Learning for Human-Computer Interaction Systems',),
}
ADDITIONAL_TOPICS = {
    'nlp': '自然語言處理', 'deep_learning': '深度學習', 'retrieval': '資訊檢索', 'ml_systems': '機器學習系統',
}


def normalize_name(value):
    return ''.join(normalize('NFKC', value).casefold().split())


def school_code(value):
    if not value:
        return None
    key = normalize_name(value)
    for code, names in SCHOOLS.items():
        if key in {normalize_name(name) for name in (code, *names)}:
            return code
    return None


SCHOOL_PREFIXES = sorted(((normalize_name(name), code) for code, names in SCHOOLS.items()
                         for name in (code, *names)), key=lambda item: -len(item[0]))


def school_prefix(target):
    for label, code in SCHOOL_PREFIXES:
        if not target.startswith(label):
            continue
        remaining = target[len(label):]
        if label.isascii() and remaining and (remaining[0] == '-' or 'a' <= remaining[0] <= 'z'):
            continue
        return remaining.removeprefix('的'), code
    return target, None


def department_names(code, name, school):
    names = {normalize_name(n) for n in (name, code)}
    title = normalize_name(name).replace('學系', '系')
    if title.endswith('工程系'):
        names.add(title.removesuffix('工程系') + '系')
    for group, (department_code, department_name, aliases) in DEPARTMENTS.items():
        if group.split('-')[0] == school and department_code == code:
            names.update(normalize_name(n) for n in (department_name, *aliases))
    return names
