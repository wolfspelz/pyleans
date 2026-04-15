.PHONY: data node web run rolling-upgrade clean-data reset-data run-single-node run-cmdline-node run-docker-node

data:
	if [ ! -d "./tmp/data" ]; then mkdir -p ./tmp/data; fi

empty-data:
	rm -rf ./tmp/data

node:
	(cd Node && dotnet publish -c Release -o app)

web:
	(cd Web && dotnet publish -c Release -o app)

run:
	make node
	make web
	make data
	docker compose up --remove-orphans

rolling-upgrade:
	bash rolling-upgrade.sh

# ------------------- Testing ------------------

clean-data:
	make empty-data

reset-data:
	make empty-data

run-single-node:
	make node
	make data
	docker compose up --remove-orphans mongodb node1

run-cmdline-node:
	make data
	(cd Node && DATA_FOLDER=$(PWD)/tmp/data dotnet run)

run-docker-node:
	make node
	make data
	docker run --rm -p 11111:11111 -p 30000:30000 -v $(PWD)/Node/app:/app -v $(PWD)/tmp/data:/data -e DATA_FOLDER=/data --name node --workdir /app --entrypoint dotnet mcr.microsoft.com/dotnet/aspnet:8.0 /app/Node.dll
