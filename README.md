# Monitor de Alertas PRTG para macOS 🚀

## Guia técnico

Consulte o [AGENTS.md](AGENTS.md) para os contratos de confiabilidade, validação e configuração segura.

A [arquitetura técnica](docs/architecture.md) documenta a reconciliação atômica de alertas, persistência de estado, validação de configuração e operação por LaunchAgent.

Este projeto é um script leve em Python que monitora ativamente seu servidor PRTG através da API XML e exibe notificações nativas na tela do seu macOS sempre que um sensor entra em estado de erro (Down) ou quando retorna ao estado normal (Up).

## ✨ Recursos

- **Zero Dependências:** Desenvolvido puramente com bibliotecas padrão do Python (`urllib`, `xml.etree`, `argparse`, `json`, etc.), compatível com `/usr/bin/python3` do macOS sem `pip install`.
- **Notificações macOS Nativas e Seguras:** Usa `osascript` (AppleScript) com passagem segura de parâmetros (`on run argv`) para disparar banners de notificação do sistema e/ou diálogos modais persistentes com comportamento distinto entre `banner`, `alert` e `both`.
- **Reconciliação Atômica e Estado Persistido:** Calcula todo o próximo estado após paginação completa e verificação de status antes de persistir atomicamente em `state.json` (com permissão `0600`) e enviar notificações, evitando alertas duplicados ou falsas resoluções após reinícios ou falhas da API.
- **Links Rápidos e Logs Seguros:** Exibe o link direto do sensor em erro no console (com cores apenas em terminais TTY), mascara tokens em logs/erros e limpa mensagens HTML quando `message_raw` está disponível.
- **Validação Estrita de Configuração:** Valida URL, token, intervalo, status monitorados e opções de notificação antes de iniciar o loop (`--check-config`).
- **Ciclo Único e Encerramento Gracioso:** Suporta `--once` para inspeções manuais e responde a `SIGTERM`/`SIGINT` para parada limpa sob LaunchAgent.

---

## 📁 Estrutura de Arquivos

- [prtg_monitor.py](prtg_monitor.py) - Script principal de monitoramento e CLI.
- [config.json.example](config.json.example) - Modelo de configuração (copie para `config.json`, ignorado pelo Git).
- [docs/architecture.md](docs/architecture.md) - Detalhes de arquitetura, reconciliação e operação.
- [AGENTS.md](AGENTS.md) - Contratos técnicos e invariantes de confiabilidade.
- [tests/test_prtg_monitor.py](tests/test_prtg_monitor.py) - Suíte de testes unitários (`unittest`).

---

## ⚙️ Configuração (`config.json`)

Copie o arquivo de exemplo para um `config.json` local e restrinja suas permissões:

```bash
cp config.json.example config.json
chmod 600 config.json
```

Edite o `config.json` com a URL do servidor e o seu token da API do PRTG:

```json
{
  "prtg_url": "https://seu-servidor-prtg.example.com",
  "apitoken": "YOUR_API_TOKEN_HERE",
  "poll_interval_seconds": 60,
  "filter_statuses": [5, 14],
  "verify_ssl": true,
  "notification_type": "banner",
  "sound_name": "Glass",
  "notify_on_resolve": true,
  "notify_on_startup": true,
  "state_file": "state.json"
}
```

> [!WARNING]
> Nunca versionar o `config.json` ou colar tokens reais de API em documentação, histórico de shell ou commits. Caso um token tenha sido exposto anteriormente, revogue-o e gere um novo no PRTG.

### Detalhes das Opções

* `prtg_url` *(obrigatório)*: URL base `https://` (ou `http://` em laboratório controlado) do servidor PRTG.
* `apitoken` *(obrigatório)*: Token de API do PRTG.
* `poll_interval_seconds` *(padrão: `60`)*: Intervalo positivo em segundos entre ciclos de verificação.
* `filter_statuses` *(padrão: `[5, 14]`)*: Lista não vazia de códigos numéricos de status PRTG a monitorar:
  * `5`: Down (Queda total)
  * `14`: Down Partial (Queda parcial)
  * `13`: Down Acknowledged (Queda reconhecida)
  * `4`: Warning (Aviso)
* `verify_ssl` *(padrão: `true`)*: Valida o certificado TLS do servidor PRTG. Defina `false` **apenas** em ambientes controlados/laboratório com certificado autoassinado.
* `notification_type` *(padrão: `"banner"`)*: Modo de notificação no macOS:
  * `"banner"`: Envia banners do Centro de Notificações (na inicialização se habilitado, em novos alertas e em resoluções).
  * `"alert"`: Exibe apenas caixas de diálogo modais (`display dialog`) para novos alertas (ícone `caution`) e resoluções (ícone `note`), sem enviar banners nem banner de inicialização.
  * `"both"`: Exibe banners do sistema **e** caixas de diálogo modais nos alertas e resoluções.
* `sound_name` *(padrão: `"Glass"`)*: Nome do som do macOS para banners de alerta (ou `null` para modo silencioso).
* `resolve_sound_name` *(opcional)*: Nome do som para banners de resolução. Quando omitido, usa `"Blow"` se `sound_name` estiver definido, ou `null` no modo silencioso.
* `notify_on_resolve` *(padrão: `true`)*: Se `true`, notifica quando um sensor previamente ativo retorna ao status `3` (`Up`).
* `notify_on_startup` *(padrão: `true`)*: Se `true` (e `notification_type` for `"banner"` ou `"both"`), envia um banner confirmando que o monitoramento iniciou.
* `state_file` *(padrão: `"state.json"`)*: Caminho do arquivo de persistência de estado (relativo ao diretório do `config.json`, ou absoluto). Use `null` para desativar a persistência em disco.
* `request_timeout_seconds` *(opcional, padrão: `15`)*: Timeout HTTP em segundos para consultas paginadas.
* `status_lookup_limit` *(opcional, padrão: `10`)*: Máximo de consultas individuais de verificação de resolução por ciclo (com rotação sem starvation).
* `status_lookup_timeout_seconds` *(opcional, padrão: `5`)*: Timeout HTTP em segundos para cada consulta individual `/api/getobjectstatus.htm`.

---

## 🚀 Como Executar e Verificar

1. Valide a configuração antes de iniciar:
```bash
python3 prtg_monitor.py --check-config config.json
```

2. Execute um único ciclo de teste para validar conectividade e parsing:
```bash
python3 prtg_monitor.py --once config.json
```

3. Inicie o monitoramento contínuo:
```bash
python3 prtg_monitor.py config.json
```

4. Execute a suíte de testes unitários:
```bash
python3 -m unittest discover -v
```

Flags disponíveis via CLI:
- `--check-config`: Valida o arquivo de configuração e sai sem consultar o PRTG.
- `--once`: Executa um único ciclo de coleta/reconciliação e encerra com código `0` (sucesso) ou `1` (falha).
- `--no-startup-notification`: Suprime o banner de inicialização nesta execução.

---

## 🔄 Executar em Segundo Plano no macOS (LaunchAgent)

Para manter o monitor rodando continuamente em segundo plano no Mac de destino:

1. Instale os arquivos em um diretório dedicado do usuário (por exemplo `/Users/<SEU_USUARIO>/prtgmon`) e verifique o caminho absoluto do Python com `which python3` (por exemplo `/usr/bin/python3` ou `/opt/homebrew/bin/python3`).
2. Crie o diretório de logs:
```bash
mkdir -p ~/Library/Logs/prtgmon
```
3. Crie `~/Library/LaunchAgents/com.prtg.monitor.plist` ajustando `<SEU_USUARIO>` e o caminho do `python3` válidos para o Mac de destino:

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
        <string>/Users/&lt;SEU_USUARIO&gt;/prtgmon/prtg_monitor.py</string>
        <string>/Users/&lt;SEU_USUARIO&gt;/prtgmon/config.json</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/Users/&lt;SEU_USUARIO&gt;/prtgmon</string>
    <key>StandardOutPath</key>
    <string>/Users/&lt;SEU_USUARIO&gt;/Library/Logs/prtgmon/monitor.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/&lt;SEU_USUARIO&gt;/Library/Logs/prtgmon/monitor_error.log</string>
</dict>
</plist>
```

4. Valide o plist e inicie o LaunchAgent:
```bash
plutil -lint ~/Library/LaunchAgents/com.prtg.monitor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```

5. Verifique a execução e os logs:
```bash
launchctl print gui/$(id -u)/com.prtg.monitor
tail -f ~/Library/Logs/prtgmon/monitor.log ~/Library/Logs/prtgmon/monitor_error.log
```

6. Para parar ou reiniciar após atualizações:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.prtg.monitor.plist
```
