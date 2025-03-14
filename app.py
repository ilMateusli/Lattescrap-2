import os
import re
import json
import pandas as pd
import time
import uuid
import threading
import glob
import platform
import random
from flask import Flask, render_template, request, jsonify, send_file, session
from flask_session import Session
from werkzeug.utils import secure_filename
from dash import Dash, dcc, html as dash_html
from dash.dependencies import Output, Input
import plotly.express as px
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from fake_useragent import UserAgent
from selenium_stealth import stealth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

server = Flask(__name__)
server.config['SECRET_KEY'] = 'alguma_chave_secreta_aqui'
server.config['UPLOAD_FOLDER'] = 'uploads'
server.config['RESULT_FOLDER'] = 'results'
server.config['ALLOWED_EXTENSIONS'] = {'xlsx', 'xls', 'csv'}
server.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

server.config['SESSION_TYPE'] = 'filesystem'
Session(server)

os.makedirs(server.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(server.config['RESULT_FOLDER'], exist_ok=True)

RESULT_FOLDER = os.path.join(BASE_DIR, 'results')
DATA_FILE_TEMPLATE = 'out_json_professores.json'

user_progress_dict = {}

# Lista global para armazenar logs
APP_LOGS = []

def log_message(msg):
    APP_LOGS.append(msg)
    print(msg)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in server.config['ALLOWED_EXTENSIONS']

def find_issn_qualis(issn: str, qualis_data: pd.DataFrame):
    linha = qualis_data[qualis_data['ISSN'] == issn]
    return linha['Estrato'].values[0] if not linha.empty else '---'

def extrair_dados(html_dict, output_dir, qualis_data):
    log_message("Iniciando extração de dados...")
    file_path = os.path.join(output_dir, DATA_FILE_TEMPLATE)
    log_message("Salvando dados JSON...")
    with open(file_path, "w", encoding='utf-8') as f:
        json.dump(html_dict, f, ensure_ascii=False, indent=4)

    info_list = []

    for professor_nome, professor_dados in html_dict.items():
        soup = BeautifulSoup(professor_dados, 'html.parser')
        nome = soup.find('h2', class_='nome').text.strip()
        log_message(f"Processando dados de {nome}.")

        try:
            bolsista_info = soup.find_all('h2', class_='nome')[1].text.strip()
        except IndexError:
            bolsista_info = '---'

        informacoes_autor = soup.find('ul', class_='informacoes-autor').find_all('li')
        endereco_cv = informacoes_autor[0].text.strip().split()[-1]
        id_lattes = informacoes_autor[1].text.strip().split(': ')[1]
        ultima_atualizacao = informacoes_autor[2].text.strip().split()[-1]
        total_artigos = len(soup.find_all('div', class_='artigo-completo'))
        total_orientacoes = str(professor_dados).count(nome + " - Coordenador")

        info_dict = {
            'Docente': nome,
            'Orientações': total_orientacoes,
            'Bolsista Info': bolsista_info,
            'Endereço CV': endereco_cv,
            'ID Lattes': id_lattes,
            'Última Atualização': ultima_atualizacao,
            'Total de Artigos': total_artigos
        }
        info_list.append(info_dict)
        log_message(f"Informações gerais de {nome} extraídas.")

    dfgeral = pd.DataFrame(info_list)
    log_message("Salvando dados gerais no Excel...")
    dfgeral_path = os.path.join(output_dir, 'DocentesPPG.xlsx')
    dfgeral.to_excel(dfgeral_path, index=False)

    # Processar artigos individuais
    for professor_nome, professor_dados in html_dict.items():
        soup = BeautifulSoup(professor_dados, 'html.parser')
        articles = soup.find_all('div', class_='artigo-completo')
        articles_info_list = []
        issn_pattern = r"issn=(\w+)"
        matches = re.findall(issn_pattern, professor_dados)
        for i, article in enumerate(articles):
            article_dict = {}
            try:
                issn = matches[i]
                issn = issn[:4] + '-' + issn[4:]
                article_dict['ISSN'] = issn
            except IndexError:
                article_dict['ISSN'] = None

            qualis_info = find_issn_qualis(article_dict['ISSN'], qualis_data)
            article_dict['Qualis'] = qualis_info
            article_dict['Índice do Artigo'] = i + 1

            citation_div = article.find('div', class_='citado')
            revista_element = article.find('img', {'class': 'ajaxJCR'})

            if citation_div and 'nomePeriodico=' in citation_div.get('cvuri', ''):
                periodico = citation_div.get('cvuri').split('nomePeriodico=')[1].split('&')[0]
                article_dict['Periódico'] = periodico
            elif revista_element and revista_element.get('original-title'):
                periodico = revista_element.get('original-title').split('<br')[0]
                article_dict['Periódico'] = periodico
            else:
                article_dict['Periódico'] = "---"

            jcr_element = article.find('span', {'data-tipo-ordenacao': 'jcr'})
            article_dict['JCR'] = jcr_element.text if jcr_element else "---"

            ano_element = article.find('span', attrs={'data-tipo-ordenacao': 'ano'})
            article_dict['Ano'] = ano_element.text if ano_element else '---'

            articles_info_list.append(article_dict)

        if articles_info_list:
            log_message(f"Salvando dados individuais de {professor_nome} no Excel...")
            df = pd.DataFrame(articles_info_list).set_index('Índice do Artigo')
            professor_excel_path = os.path.join(output_dir, f"{professor_nome}.xlsx")
            df.to_excel(professor_excel_path)
        else:
            log_message(f"Nenhum artigo encontrado para {professor_nome}.")

    log_message("Extração de dados concluída.")

def wait_and_find(driver, by_what, identifier, timeout=10):
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by_what, identifier)))

def get_html(driver, proff):
    log_message(f"Pesquisando docente: {proff}")
    url = "http://buscatextual.cnpq.br/buscatextual/busca.do"
    driver.get(url)
    time.sleep(random.uniform(2, 4))  # Atraso aleatório para mimetizar comportamento humano

    search_bar = wait_and_find(driver, By.ID, 'textoBusca')
    search_bar.clear()  # Limpa a barra de busca antes de inserir
    search_bar.send_keys(proff)
    time.sleep(random.uniform(1, 3))  # Atraso aleatório
    search_bar.send_keys(Keys.ENTER)

    try:
        link = wait_and_find(driver, By.XPATH, f'//a[starts-with(@href, "javascript:abreDetalhe")]')
    except:
        log_message(f"Não encontrou resultado para {proff}")
        return ""
    time.sleep(random.uniform(1, 2))
    link.click()

    try:
        btn_abre_curriculo = wait_and_find(driver, By.ID, "idbtnabrircurriculo")
    except:
        log_message(f"Botão de abrir currículo não encontrado para {proff}")
        return ""
    time.sleep(random.uniform(1, 3))
    btn_abre_curriculo.click()
    time.sleep(random.uniform(1, 3))

    # Alterna para a nova janela/tab
    driver.switch_to.window(driver.window_handles[1])
    log_message(f"Abrindo resultado para {proff}")
    html = driver.page_source
    log_message(f"Guardando informações para {proff}")
    driver.close()
    driver.switch_to.window(driver.window_handles[0])

    return html

def fetch_proxies():
    proxies = []
    proxies_file_path = os.path.join(BASE_DIR, 'proxies.txt')

    if os.path.exists(proxies_file_path):
        with open(proxies_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    proxies.append(line)
    else:
        log_message(f"Arquivo proxies.txt não encontrado em {proxies_file_path}")

    return proxies
def initialize_webdriver(proxy=None):
    ua = UserAgent()
    user_agent = ua.random  # Gera um User-Agent aleatório

    if platform.system() == 'Windows':
        windows_driver_path = "chromedriver.exe"
        service = Service(windows_driver_path)
    else:
        linux_driver_path = "/usr/bin/chromedriver"
        service = Service(linux_driver_path)
    
    chrome_options = Options()
    # chrome_options.add_argument("--headless")  # Evite usar headless se possível
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    # chrome_options.add_argument("--incognito")  # Pode ser útil, mas depende do site
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"user-agent={user_agent}")  # Define o User-Agent falso

    # Configurações para evitar detecção de automação
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    # Remover a adição do proxy
    # if proxy:
    #     chrome_options.add_argument(f"--proxy-server={proxy}")

    driver = webdriver.Chrome(service=service, options=chrome_options)

    # Implementa stealth para ocultar sinais de automação
    stealth(driver,
            languages=["pt-BR", "pt"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
            )

    # Remove a propriedade 'navigator.webdriver'
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    return driver


def get_htmls(professors, user_id):
    log_message("Iniciando coleta de HTMLs dos professores...")

    # Obter lista de proxies
    proxies = fetch_proxies()

    # Escolher um proxy aleatório (ou None se não houver)
    if proxies:
        proxy = random.choice(proxies)
        log_message(f"Usando proxy: {proxy}")
    else:
        proxy = None
        log_message("Nenhum proxy encontrado, prosseguindo sem proxy.")

    # Inicializa apenas uma vez o WebDriver com o proxy selecionado
    driver = initialize_webdriver(proxy=proxy)

    html_dict = {}
    errored_professors = []
    final_error = []

    total = len(professors)
    user_progress_dict[user_id] = {'current': 0, 'total': total, 'status': 'running'}

    for index, proff in enumerate(professors, 1):
        driver.delete_all_cookies()
        log_message(f"Procurando: {proff}")
        try:
            html = get_html(driver, proff)
            if html:
                html_dict[proff] = html
            else:
                errored_professors.append(proff)
        except Exception as e:
            log_message(f'Erro com o professor: {proff}. Detalhes: {str(e)}')
            errored_professors.append(proff)

        # Atualizar progresso
        user_progress = user_progress_dict.get(user_id, {'current': 0, 'total': total, 'status': 'running'})
        user_progress['current'] = index
        user_progress_dict[user_id] = user_progress

        if index % 3 == 0:
            log_message("Deletando Cookies")
            driver.delete_all_cookies()
            # Opcional: Reiniciar o driver para limpar cache e outros dados
            driver.quit()
            # Re-inicializa o driver
            proxy = random.choice(proxies) if proxies else None
            driver = initialize_webdriver(proxy=proxy)
            log_message("Driver reiniciado para limpar cache e evitar detecção.")
            time.sleep(random.uniform(2, 5))  # Aguardar um tempo antes de continuar

    # Reprocessar professores com erro
    while errored_professors:
        proff = errored_professors.pop()
        try:
            log_message(f"Tentando novamente: {proff}")
            html = get_html(driver, proff)
            if html:
                html_dict[proff] = html
            else:
                final_error.append(proff)
        except Exception as e:
            log_message(f'Erro repetido com o professor: {proff}. Detalhes: {str(e)}')
            final_error.append(proff)

        # Atualizar progresso (conta mais uma tentativa)
        user_progress = user_progress_dict.get(user_id, {'current': 0, 'total': total, 'status': 'running'})
        user_progress['current'] += 1
        user_progress_dict[user_id] = user_progress

    if final_error:
        error_message = "\n".join(final_error)
        log_message(f"Não foi encontrado: \n{error_message}")

    driver.quit()
    log_message("Coleta de HTMLs concluída.")

    return html_dict

def process_file(file_path_nomes, file_path_classificacoes, output_dir, selected_column, user_id):
    if user_id not in user_progress_dict:
        user_progress_dict[user_id] = {'current': 0, 'total': 0, 'status': 'idle'}

    log_message("Carregando dados de Classificações...")
    try:
        qualis_data = pd.read_excel(file_path_classificacoes)[['ISSN', 'Título', 'Estrato']]
        log_message("Dados de Classificações carregados.")
    except Exception as e:
        log_message(f"Erro ao carregar dados de Classificações: {str(e)}")
        qualis_data = pd.DataFrame(columns=['ISSN', 'Título', 'Estrato'])

    log_message("Carregando lista de professores...")
    try:
        all_cols = pd.read_excel(file_path_nomes).columns
        prof_column = all_cols[int(selected_column)]
        professors = pd.read_excel(file_path_nomes)[prof_column].dropna().tolist()
        log_message(f"Lista de professores carregada: {len(professors)} professores.")
    except Exception as e:
        log_message(f"Erro ao carregar lista de professores: {str(e)}")
        professors = []

    if not professors:
        log_message("Nenhum professor encontrado.")
        user_progress_dict[user_id]['status'] = 'error'
        return

    user_progress_dict[user_id]['total'] = len(professors)
    user_progress_dict[user_id]['current'] = 0
    user_progress_dict[user_id]['status'] = 'running'

    html_dict = get_htmls(professors, user_id)

    extrair_dados(html_dict, output_dir, qualis_data)

    log_message(f"Processamento concluído. Resultados salvos em {output_dir}")
    user_progress_dict[user_id]['status'] = 'done'

def load_user_data(user_id):
    user_folder = os.path.join(RESULT_FOLDER, user_id)
    doc_path = os.path.join(user_folder, 'DocentesPPG.xlsx')

    if not os.path.exists(doc_path):
        log_message(f"DocentesPPG.xlsx não encontrado para {user_id}")
        return pd.DataFrame()

    df_docentes = pd.read_excel(doc_path)

    # Carregar dados individuais
    professor_files = glob.glob(os.path.join(user_folder, '*.xlsx'))
    individual_dfs = []
    for file in professor_files:
        if os.path.basename(file) == 'DocentesPPG.xlsx':
            continue
        df = pd.read_excel(file)
        professor_nome = os.path.splitext(os.path.basename(file))[0]
        df['Docente'] = professor_nome
        individual_dfs.append(df)

    if individual_dfs:
        df_individual = pd.concat(individual_dfs, ignore_index=True)
        df_merged = pd.merge(df_individual, df_docentes, on='Docente', how='left')
        log_message("Dados individuais dos professores mesclados com dados gerais.")
        return df_merged
    else:
        log_message("Sem dados individuais para mesclar. Retornando dados gerais.")
        return df_docentes

dash_app = Dash(
    __name__,
    server=server,
    url_base_pathname='/dash/',
    suppress_callback_exceptions=True,
)

dash_app.layout = dash_html.Div([
    dash_html.H1("Dashboard de Resultados"),

    dcc.Store(id='store-user-id'),
    dcc.Store(id='store-data'),

    dash_html.Div([
        dash_html.Label("Intervalo de Anos:"),
        dcc.RangeSlider(
            id='year-slider',
            step=1,
            value=[2000, 2025],
            tooltip={"placement": "bottom", "always_visible": True},
        ),
        dash_html.Label("Professores:"),
        dcc.Dropdown(id='professor-dropdown', multi=True),
    ], style={'width': '30%', 'float': 'left', 'display': 'inline-block', 'verticalAlign': 'top'}),

    dash_html.Div([
        dcc.Graph(id='bar-chart-docentes'),
        dcc.Graph(id='line-chart-publicacoes'),
        dcc.Graph(id='bar-chart-periodicos'),
        dcc.Graph(id='bar-chart-qualis'),
        dcc.Graph(id='line-chart-professors'),
        dcc.Graph(id='line-chart-evolucao'),
    ], style={'width': '70%', 'float': 'right', 'display': 'inline-block'})
])

@dash_app.callback(
    Output('store-user-id', 'data'),
    Input('year-slider', 'id')
)
def get_user_id(_):
    return session.get('user_id', '')

@dash_app.callback(
    Output('store-data', 'data'),
    Input('store-user-id', 'data')
)
def load_user_data_callback(user_id):
    if not user_id:
        return {}
    df = load_user_data(user_id)
    if df.empty:
        return {}
    return df.to_dict('records')

@dash_app.callback(
    [
        Output('professor-dropdown', 'options'),
        Output('professor-dropdown', 'value'),
        Output('year-slider', 'min'),
        Output('year-slider', 'max'),
        Output('year-slider', 'marks'),
        Output('year-slider', 'value'),
    ],
    Input('store-data', 'data')
)
def update_filters(data):
    if not data:
        return [], [], 2000, 2025, {}, [2000, 2025]

    df = pd.DataFrame(data)
    professores = df['Docente'].unique()
    professor_options = [{'label': p, 'value': p} for p in professores]

    if 'Ano' in df.columns and df['Ano'].notna().any():
        df['Ano'] = pd.to_numeric(df['Ano'], errors='coerce')
        df = df.dropna(subset=['Ano'])
        if df.empty:
            return professor_options, list(professores), 2000, 2025, {}, [2000, 2025]
        min_year = int(df['Ano'].min())
        max_year = int(df['Ano'].max())
        marks = {year: str(year) for year in range(min_year, max_year+1, 5)}
        initial_value = [min_year, max_year]
    else:
        min_year = 2000
        max_year = 2025
        marks = {}
        initial_value = [2000, 2025]

    return professor_options, list(professores), min_year, max_year, marks, initial_value

@dash_app.callback(
    [
        Output('bar-chart-docentes', 'figure'),
        Output('line-chart-publicacoes', 'figure'),
        Output('bar-chart-periodicos', 'figure'),
        Output('bar-chart-qualis', 'figure'),
        Output('line-chart-professors', 'figure'),
        Output('line-chart-evolucao', 'figure'),
    ],
    [
        Input('store-data', 'data'),
        Input('year-slider', 'value'),
        Input('professor-dropdown', 'value')
    ]
)
def update_graphs(data, year_range, selected_professors):
    if not data:
        return [{}]*6
    df = pd.DataFrame(data)
    if 'Ano' in df.columns:
        df['Ano'] = pd.to_numeric(df['Ano'], errors='coerce')
        df = df.dropna(subset=['Ano'])
        if not df.empty:
            df = df[df['Ano'].between(year_range[0], year_range[1], inclusive='both')]

    if selected_professors:
        df = df[df['Docente'].isin(selected_professors)]

    if 'Total de Artigos' in df.columns and 'Docente' in df.columns:
        docentes_artigos = df[['Docente', 'Total de Artigos']].drop_duplicates().copy()
        fig1 = px.bar(docentes_artigos, x='Docente', y='Total de Artigos', title='Número de Artigos por Docente')
    else:
        fig1 = px.bar(title='Dados de Artigos não disponíveis')

    if 'Orientações' in df.columns and 'Docente' in df.columns:
        docentes_orientacoes = df[['Docente', 'Orientações']].drop_duplicates()
        fig2 = px.line(docentes_orientacoes, x='Docente', y='Orientações', title='Orientações por Docente')
    else:
        fig2 = px.line(title='Dados de Orientações não disponíveis')

    if 'Periódico' in df.columns and df['Periódico'].notna().any():
        top_periodicos = df['Periódico'].value_counts().nlargest(10).reset_index()
        top_periodicos.columns = ['Periódico', 'Número de Artigos']
        fig3 = px.bar(top_periodicos, x='Periódico', y='Número de Artigos', title='Top 10 Periódicos por Número de Artigos')
    else:
        fig3 = px.bar(title='Dados de Periódicos não disponíveis')

    if 'Qualis' in df.columns and df['Qualis'].notna().any():
        qualis_count = df['Qualis'].value_counts().reset_index()
        qualis_count.columns = ['Qualis', 'Número de Artigos']
        fig4 = px.bar(qualis_count, x='Qualis', y='Número de Artigos', title='Distribuição de Artigos por Qualis')
    else:
        fig4 = px.bar(title='Dados de Qualis não disponíveis')

    if 'Ano' in df.columns and df['Ano'].notna().any():
        pub_ano = df['Ano'].value_counts().sort_index().reset_index()
        pub_ano.columns = ['Ano', 'Número de Publicações']
        fig5 = px.line(pub_ano, x='Ano', y='Número de Publicações', title='Publicações por Ano')
    else:
        fig5 = px.line(title='Dados de Ano não disponíveis')

    if 'Ano' in df.columns and 'Docente' in df.columns:
        df_evolucao = df.groupby(['Ano', 'Docente']).size().reset_index(name='Número de Publicações')
        if not df_evolucao.empty:
            fig6 = px.line(df_evolucao, x='Ano', y='Número de Publicações', color='Docente',
                           title='Evolução de Publicações ao Longo dos Anos por Docente')
        else:
            fig6 = px.line(title='Dados de Evolução não disponíveis')
    else:
        fig6 = px.line(title='Dados de Evolução não disponíveis')

    return fig1, fig2, fig3, fig4, fig5, fig6

@server.route('/')
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        user_progress_dict[session['user_id']] = {'current': 0, 'total': 0, 'status': 'idle'}
    return render_template('index.html')

@server.route('/upload', methods=['POST'])
def upload_file():
    if 'nomes_file' not in request.files or 'classificacoes_file' not in request.files:
        return jsonify({'status': 'error', 'message': 'Ambos os arquivos devem ser enviados.'}), 400

    nomes_file = request.files['nomes_file']
    classificacoes_file = request.files['classificacoes_file']

    if nomes_file.filename == '' or classificacoes_file.filename == '':
        return jsonify({'status': 'error', 'message': 'Nenhum arquivo selecionado.'}), 400

    if nomes_file and allowed_file(nomes_file.filename) and classificacoes_file and allowed_file(classificacoes_file.filename):
        nomes_filename = secure_filename(nomes_file.filename)
        classificacoes_filename = secure_filename(classificacoes_file.filename)

        user_id = session['user_id']
        user_upload_folder_nomes = os.path.join(server.config['UPLOAD_FOLDER'], 'nomes', user_id)
        user_upload_folder_classificacoes = os.path.join(server.config['UPLOAD_FOLDER'], 'classificacoes', user_id)
        user_result_folder = os.path.join(server.config['RESULT_FOLDER'], user_id)

        os.makedirs(user_upload_folder_nomes, exist_ok=True)
        os.makedirs(user_upload_folder_classificacoes, exist_ok=True)
        os.makedirs(user_result_folder, exist_ok=True)

        nomes_path = os.path.join(user_upload_folder_nomes, nomes_filename)
        classificacoes_path = os.path.join(user_upload_folder_classificacoes, classificacoes_filename)
        nomes_file.save(nomes_path)
        classificacoes_file.save(classificacoes_path)
        log_message(f"Arquivos salvos para usuário {user_id}.")

        selected_column = request.form.get('column')
        if selected_column is None:
            return jsonify({'status': 'error', 'message': 'Coluna não selecionada.'}), 400

        log_message("Iniciando thread de processamento.")
        thread = threading.Thread(target=process_file, args=(nomes_path, classificacoes_path, user_result_folder, selected_column, user_id))
        thread.start()

        return jsonify({'status': 'success', 'message': 'Arquivos enviados e processamento iniciado.'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Tipos de arquivos não permitidos.'}), 400

def generate_consolidated_json(user_id):
    user_folder = os.path.join(RESULT_FOLDER, user_id)
    docentes_path = os.path.join(user_folder, 'DocentesPPG.xlsx')

    if not os.path.exists(docentes_path):
        log_message(f"Arquivo DocentesPPG.xlsx não encontrado para {user_id}.")
        return {}

    df_docentes = pd.read_excel(docentes_path)
    docentes_data = df_docentes.to_dict('records')

    professor_files = glob.glob(os.path.join(user_folder, '*.xlsx'))
    individual_data = {}
    for file in professor_files:
        if os.path.basename(file) == 'DocentesPPG.xlsx':
            continue
        professor_nome = os.path.splitext(os.path.basename(file))[0]
        df_individual = pd.read_excel(file)
        individual_data[professor_nome] = df_individual.to_dict('records')

    consolidated_data = {
        'docentes': docentes_data,
        'individual_data': individual_data
    }
    return consolidated_data

@server.route('/download', methods=['GET'])
def download_file_endpoint():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Sessão inválida.'}), 400

    consolidated_data = generate_consolidated_json(user_id)
    if not consolidated_data:
        return jsonify({'status': 'error', 'message': 'Nenhum dado disponível para download.'}), 404

    result_file_path = os.path.join(RESULT_FOLDER, user_id, 'resultados_consolidados.json')
    with open(result_file_path, 'w', encoding='utf-8') as json_file:
        json.dump(consolidated_data, json_file, ensure_ascii=False, indent=4)

    return send_file(result_file_path, as_attachment=True)

@server.route('/progress', methods=['GET'])
def get_progress():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Sessão inválida.'}), 400

    progress = user_progress_dict.get(user_id, {'current': 0, 'total': 0, 'status': 'idle'})
    return jsonify({
        'status': progress['status'],
        'current': progress['current'],
        'total': progress['total']
    })

@server.route('/logs', methods=['GET'])
def get_logs():
    return jsonify(APP_LOGS)

if __name__ == '__main__':
    server.run(debug=True, port=5000)
