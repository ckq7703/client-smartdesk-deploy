import os
import re
import requests
import unicodedata
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "smartdesk-saas-secret")

# Portainer Configuration
PORTAINER_URL = os.getenv("PORTAINER_URL", "https://portainer.smartpro.com.vn")
PORTAINER_TOKEN = os.getenv("PORTAINER_TOKEN")
PORTAINER_ENV_ID = os.getenv("PORTAINER_ENV_ID", "1")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "smartpro.com.vn")
API_SECRET = os.getenv("API_SECRET", "your-secret-key-here")

# Docker Compose Template
SMARTDESK_COMPOSE_TEMPLATE = """
networks:
  tenant-internal:
    driver: bridge
  saas-network:
    external: true

volumes:
  app_config:
    name: ${TENANT_SLUG}_config
  app_files:
    name: ${TENANT_SLUG}_files
  app_plugins:
    name: ${TENANT_SLUG}_plugins
  app_db:
    name: ${TENANT_SLUG}_db

services:
  app:
    image: quochigh/smartdesk-v2026.1
    container_name: tenant_${TENANT_SLUG}_app
    restart: always
    environment:
      - DB_HOST=db
      - DB_USER=${TENANT_SLUG}_user
      - DB_PASSWORD=${DB_PASSWORD}
      - DB_NAME=${TENANT_SLUG}_db
      - WAIT_FOR_DB=true
      - FORCE_DB_INSTALL=false
    volumes:
      - app_config:/var/www/html/config
      - app_files:/var/www/html/files
      - app_plugins:/var/www/html/plugins
    networks:
      - tenant-internal
      - saas-network
    depends_on:
      db:
        condition: service_healthy
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.${TENANT_SLUG}.rule=Host(`${TENANT_SLUG}.smartpro.com.vn`)"
      - "traefik.http.routers.${TENANT_SLUG}.entrypoints=web"
      - "traefik.http.services.${TENANT_SLUG}.loadbalancer.server.port=80"
      - "traefik.docker.network=saas-network"

  db:
    image: mariadb:10.11
    container_name: tenant_${TENANT_SLUG}_db
    restart: always
    environment:
      - MARIADB_ROOT_PASSWORD=${DB_ROOT_PASSWORD}
      - MARIADB_DATABASE=${TENANT_SLUG}_db
      - MARIADB_USER=${TENANT_SLUG}_user
      - MARIADB_PASSWORD=${DB_PASSWORD}
    volumes:
      - app_db:/var/lib/mysql
    networks:
      - tenant-internal
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
"""

def slugify(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)

def portainer_request(endpoint, method="GET", data=None, params=None):
    url = f"{PORTAINER_URL.rstrip('/')}/api{endpoint}"
    headers = {"X-API-Key": PORTAINER_TOKEN}
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            params=params,
            timeout=180
        )
        
        if 'application/json' in response.headers.get('Content-Type', ''):
            return response.json(), response.status_code
        return response.text, response.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def get_container_logs(slug):
    docker_endpoint = f"/endpoints/{PORTAINER_ENV_ID}/docker/containers"
    
    # Filter containers by labels
    filters = f'{{"label":["com.docker.compose.project=tenant-{slug}", "com.docker.compose.service=app"]}}'
    result, status = portainer_request(f"{docker_endpoint}/json", params={"filters": filters})
    
    if status != 200 or not isinstance(result, list) or len(result) == 0:
        return None, "Đang chuẩn bị container..."

    container_id = result[0].get("Id")
    if not container_id:
        return None, "Không tìm thấy Container ID"

    # Get logs
    logs_endpoint = f"{docker_endpoint}/{container_id}/logs"
    logs, log_status = portainer_request(logs_endpoint, params={"stdout": 1, "stderr": 1, "tail": 50})
    
    if log_status != 200:
        return None, "Đang kết nối để đọc logs..."

    # Readiness check
    if "apache2 -D FOREGROUND" in logs or "resuming normal operations" in logs:
        return "ready", "Hệ thống đã sẵn sàng!"
    
    if "Importing custom template database" in logs or "Ensuring permissions after import" in logs:
        return "initializing_db", "Đang cấu hình Cơ sở dữ liệu (Database)..."
    
    if "Database is ready!" in logs:
        return "db_ready", "Cơ sở dữ liệu đã sẵn sàng, đang khởi động ứng dụng..."

    return "starting", "Container đã chạy, đang khởi động dịch vụ bên trong..."

@app.route('/')
def index():
    return render_template('index.html', api_secret=API_SECRET)

@app.route('/provision', methods=['POST'])
def provision():
    if API_SECRET and API_SECRET != "none":
        if request.headers.get("X-Provisioner-Key") != API_SECRET:
            return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.json
    company = data.get("company")
    email = data.get("email")
    
    if not company or not email:
        return jsonify({"success": False, "error": "Tên công ty và Email là bắt buộc"}), 400

    slug = slugify(company)
    db_password = os.urandom(8).hex()
    root_password = os.urandom(12).hex()

    url = f"/stacks/create/standalone/string?endpointId={PORTAINER_ENV_ID}"
    payload = {
        "name": f"tenant-{slug}",
        "stackFileContent": SMARTDESK_COMPOSE_TEMPLATE,
        "env": [
            {"name": "TENANT_SLUG", "value": slug},
            {"name": "DB_PASSWORD", "value": db_password},
            {"name": "DB_ROOT_PASSWORD", "value": root_password}
        ]
    }

    result, status = portainer_request(url, method="POST", data=payload)
    
    if status == 200 or status == 201:
        return jsonify({
            "success": True,
            "slug": slug,
            "url": f"https://{slug}.{BASE_DOMAIN}",
            "message": "Bắt đầu khởi tạo, vui lòng đợi..."
        }), 201
    else:
        return jsonify({"success": False, "error": result}), status

@app.route('/status/<slug>')
def status(slug):
    # 1. Check Stack
    result, status_code = portainer_request("/stacks")
    if status_code != 200:
        return jsonify({"ready": False, "message": "Không thể kết nối danh sách Stacks"}), status_code
    
    stack_name = f"tenant-{slug}"
    stack_found = False
    for stack in result:
        if stack.get("Name") == stack_name:
            if stack.get("Status") != 1:
                return jsonify({"ready": False, "message": "Hệ thống đang được dựng..."})
            stack_found = True
            break
    
    if not stack_found:
        return jsonify({"ready": False, "message": "Không tìm thấy hệ thống đăng ký"}), 404

    # 2. Check Logs
    state, message = get_container_logs(slug)
    
    return jsonify({
        "ready": state == "ready",
        "state": state,
        "message": message,
        "url": f"https://{slug}.{BASE_DOMAIN}"
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)
