# Orleans Virtual Actor Rolling Upgrade Sample

Also a good staring point for Orleans.

## As a Starting Point

To run it follow the demo (you'll need docker)

### Debugging

For a simple debug session, run the task "Debug Web API (Local Silo)". This starts a web frontend with integrated silo node as a single process where you can debug the Web frontend and the application logic.

### Membership

Included is an example usage of the mongodb membership management. The web frontend is a separate process that accesses the silo nodes via the mongodb membership table. For production you'd use a real (web scale) membership provider like zookeper or a cloud table. 

### Streaming

There is no streaming configured yet, because not needed by the demo. A simple built-in streaming provider can be added with just 2 lines of code (copilot: "add streaming"). For production you'd use a real (web scale) streaming provider like MSMQ or kafka. 

### Storage

A JSON file based storage is included for demonstration purposes. For production you'd use a real (web scale) storage provider, a cloud based document DB or a cloud table storage or your own hyperscale SQL solution. 

## The Demo

Terminal 1 (starts: 9 nodes + mongodb for membership table + a web frontend on http-localhost-5000):
```
make run
```

Browser
- http://localhost:5000
- get the grid
- select fields
- activate auto-refresh

Change the business logic to show a changed implementation
- in Logic/Field.cs
- change _color value 

Terminal 2:
```
make node
make rolling-upgrade
```

Also Swagger: http://localhost:5000/swagger

## Screenshot

![Demo Screen](assets/DemoScreenshot.png?raw=true "Demo Screen")
