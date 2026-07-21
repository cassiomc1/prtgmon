# Monitor de Alertas PRTG para macOS 🚀

## Guia técnico

Consulte o [AGENTS.md](AGENTS.md) para os contratos de confiabilidade, validação e configuração segura.

A [arquitetura técnica](docs/architecture.md) documenta a reconciliação de alertas, configuração e operação por LaunchAgent.

Este projeto é um script leve em Python que monitora ativamente seu servidor PRTG através da API HTTP/REST e exibe notificações nativas na tela do seu macOS sempre que um sensor entra em estado de erro (Down) ou quando o erro é resolvido.

## ✨ Recursos

- **Zero Dependências:** Desenvolvido puramente com bibliotecas nativas do Python (`urllib`, `xml.etree`, etc.). Não necessita de `pip install`.
- **Notificações macOS Nativas:** Usa `osascript` (AppleScript) para disparar banners de notificação do sistema ou caixas de alerta persistentes com som.
- **Prevenção de Spam de Alertas:** Mantém um histórico em memória dos alertas ativos. O script notifica apenas uma vez quando o sensor cai e quando ele retorna à normalidade (resolvido).
- **Links Rápidos no Terminal:** Exibe o link direto do sensor em erro no console, facilitando a ação rápida.
- **Configuração Simples:** Configurações centralizadas em um arquivo `config.json`.
- **Suporte a Certificados Autoassinados:** Permite desabilitar a verificação SSL (`verify_ssl: false`), útil para servidores PRTG locais.

---

## 📁 Estrutura de Arquivos

O projeto contém os seguintes arquivos:
- [prtg_monitor.py](file:///Users/cassio/Downloads/github/prtgmon/prtg_monitor.py) - O script Python principal que executa o monitoramento.
- [config.json](file:///Users/cassio/Downloads/github/prtgmon/config.json) - O arquivo contendo as configurações de URL, token, intervalo e notificações.

---

## ⚙️ Configuração (`config.json`)

Edite o arquivo [config.json](file:///Users/cassio/Downloads/github/prtgmon/config.json) para incluir as credenciais do seu servidor PRTG:

```json
{
  "prtg_url": "https://seu-servidor-prtg.com",
  "apitoken": "***REMOVED***",
  "poll_interval_seconds": 60,
  "filter_statuses": [5, 14],
  "verify_ssl": false,
  "notification_type": "banner",
  "sound_name": "Glass",
  "notify_on_resolve": true
}
```

### Detalhes das Opções:
*   `prtg_url`: A URL base do seu servidor PRTG (ex: `https://192.168.1.100` ou `https://prtg.minhaempresa.com`).
*   `apitoken`: O seu Token de API do PRTG (o token que você forneceu já está pré-configurado).
*   `poll_interval_seconds`: Tempo em segundos entre as checagens no servidor PRTG (padrão: `60` segundos).
*   `filter_statuses`: Lista de códigos de status para monitorar.
    *   `5`: Down (Queda total)
    *   `14`: Down Partial (Queda parcial)
    *   `13`: Down Acknowledged (Queda reconhecida)
    *   `4`: Warning (Aviso)
*   `verify_ssl`: Defina como `false` se o seu servidor PRTG usa um certificado SSL autoassinado/inválido.
*   `notification_type`: Tipo de alerta no macOS:
    *   `"banner"`: Notificação padrão do macOS no canto superior direito (desliza e some).
    *   `"alert"`: Caixa de diálogo/modal no centro da tela que requer um clique em "OK".
    *   `"both"`: Exibe ambos.
*   `sound_name`: Som a ser reproduzido com a notificação (ex: `"Glass"`, `"Ping"`, `"Basso"`, `"Blow"`, `"Frog"`).
*   `notify_on_resolve`: Se `true`, envia uma notificação verde quando o sensor voltar a funcionar.

---

## 🚀 Como Executar

### Passo 1: Configurar a URL do PRTG
Substitua `"https://your-prtg-server.example.com"` no [config.json](file:///Users/cassio/Downloads/github/prtgmon/config.json) pela URL real do seu servidor PRTG.

### Passo 2: Executar o Script
No terminal do macOS, navegue até a pasta do projeto e execute:
```bash
python3 prtg_monitor.py
```

Ou torne o arquivo executável e rode-o diretamente:
```bash
./prtg_monitor.py
```

---

## 🔄 Executar em Segundo Plano no macOS (LaunchAgent)

Se você deseja que este script rode sempre em segundo plano de forma contínua no seu Mac, a melhor forma é configurar um LaunchAgent do macOS.

1. Crie um arquivo chamado `com.prtg.monitor.plist` na pasta `~/Library/LaunchAgents/` com o seguinte conteúdo:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.prtg.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/cassio/Downloads/github/prtgmon/prtg_monitor.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/Users/cassio/Downloads/github/prtgmon</string>
    <key>StandardOutPath</key>
    <string>/Users/cassio/Downloads/github/prtgmon/monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/cassio/Downloads/github/prtgmon/monitor_error.log</string>
</dict>
</plist>
```

2. Carregue o agente para iniciar o monitoramento automático em segundo plano:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```

3. Para parar o monitoramento:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```
