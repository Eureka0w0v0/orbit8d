.PHONY: setup assets dev backend web test test-py test-web lint build run

BACKEND := backend
WEB := web

setup:            ## 安装前后端依赖并下载 HRTF 数据
	cd $(BACKEND) && uv sync
	cd $(WEB) && npm install
	$(MAKE) assets

assets:           ## 下载 KU100 全球面 HRTF（约 20 MB，校验 sha256）
	cd $(BACKEND) && uv run python -m orbit8d.assets

dev:              ## 同时启动后台（8765）和前端开发服务器（5173）
	$(MAKE) -j2 backend web

backend:
	cd $(BACKEND) && uv run python -m orbit8d --no-browser

web:
	cd $(WEB) && npm run dev

test: test-py test-web   ## 全量测试

test-py:
	cd $(BACKEND) && uv run pytest -q

test-web:
	cd $(WEB) && npm run typecheck && npm test

lint:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

build:            ## 构建前端，产物由后台托管
	cd $(WEB) && npm run build

run: build        ## 正式使用：构建前端后启动，自动打开浏览器
	cd $(BACKEND) && uv run python -m orbit8d
