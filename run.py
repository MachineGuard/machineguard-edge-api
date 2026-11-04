from edge_api import create_app

app = create_app()

if __name__ == "__main__":
    try:
        app.run(host=app.config['services'].config.bind_host, port=5000)
    finally:
        app.config['services'].runtime.stop()
