.PHONY: deploy up seed test down
deploy:
	./deploy.sh

up:
	docker compose up --build -d
seed:
	docker compose exec api python -m app.seed --scenario add-drop
test:
	docker compose run --rm migrate python run_tests.py

down:
	docker compose down
