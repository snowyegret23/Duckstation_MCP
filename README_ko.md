# DuckStation MCP

**[ENGLISH](README.md) | KOREAN**

DuckStation MCP는 [DuckStation](https://github.com/stenzek/duckstation)의 내장 GDB Remote Serial Protocol 서버를 통해 PlayStation 게임을 디버깅하는 Windows 중심의 Model Context Protocol 서버입니다. 실행 제어, 레지스터 접근, 메모리 조사, 브레이크포인트, 프로세스 관리, 로그 분석 기능을 MCP 도구로 제공합니다.

> [!IMPORTANT]
> MCP 서버를 연결하기 전에 DuckStation의 GDB 서버를 활성화하고 게임을 불러와야 합니다. 이 프로젝트는 DuckStation의 기존 GDB 서버를 사용하므로 DuckStation 소스를 수정하거나 다시 빌드할 필요가 없습니다.

## 주요 기능

- DuckStation GDB 서버 연결 및 연결 상태 확인
- 에뮬레이션 일시 정지, 재개, 단일 스텝
- MIPS 레지스터 조회 및 지원 레지스터 이름 확인
- 에뮬레이션 메모리 읽기, 쓰기, 덤프
- RAM, 스크래치패드, BIOS 프리셋 영역 덤프
- 실행, 읽기, 쓰기, 접근 브레이크포인트 추가 및 제거
- 레벨, 채널, 문자열을 기준으로 DuckStation 로그 조회 및 필터링
- 파일 잠금을 해제하기 위한 DuckStation 프로세스 조회 및 종료

## 요구 사항

- Windows
- Python 3.10 이상
- GDB 서버가 활성화된 DuckStation
- 로컬 stdio 서버를 지원하는 MCP 클라이언트

기본 GDB 연결 주소는 `127.0.0.1:19000`입니다.

## DuckStation 설정

DuckStation의 Windows 설정 파일은 일반적으로 `%LOCALAPPDATA%\DuckStation\settings.ini`에 있습니다. GDB 서버와 파일 로그를 활성화합니다.

```ini
[Debug]
EnableGDBServer = true
GDBServerPort = 19000

[Logging]
LogToFile = true
```

GUI에서는 **Settings > Advanced > Debugging (Tweaks)**에서 같은 옵션을 설정할 수 있습니다. 설정을 변경한 뒤 DuckStation을 다시 시작하고, `connect`를 호출하기 전에 게임을 불러옵니다.

기본 로그 경로는 `%LOCALAPPDATA%\DuckStation\duckstation.log`입니다.

## 설치

PowerShell에서 저장소 폴더로 이동한 뒤 실행합니다.

```powershell
python -m pip install -e .
```

서버를 직접 실행하려면 다음 명령을 사용합니다.

```powershell
python -m duckstation_mcp
```

서버는 stdio로 통신하므로 일반적으로 독립형 대화형 터미널이 아니라 MCP 클라이언트에서 실행합니다.

## Codex MCP 설정

DuckStation MCP를 Codex의 로컬 stdio 서버로 등록하려면 `%USERPROFILE%\.codex\config.toml`에 다음 블록을 추가합니다. `C:\path\to\Duckstation_MCP`는 이 저장소를 복제한 실제 경로로 변경하십시오.

```toml
[mcp_servers.duckstation]
command = "python"
args = ["-m", "duckstation_mcp"]
cwd = 'C:\path\to\Duckstation_MCP'

[mcp_servers.duckstation.env]
PYTHONPATH = 'C:\path\to\Duckstation_MCP\src'
```

`python` 명령은 프로젝트 의존성을 설치한 Python 인터프리터를 가리켜야 합니다. `config.toml`을 변경한 뒤 Codex를 다시 시작합니다.

## 일반적인 작업 순서

1. DuckStation GDB 서버를 활성화하고 게임을 불러옵니다.
2. 포트 `19000`으로 `connect`를 호출합니다. 기본값 `auto_resume=true`는 새 GDB 클라이언트 연결로 일시 정지된 에뮬레이션을 다시 시작합니다.
3. 레지스터를 조사하거나 단일 스텝을 실행하기 전에 `pause`를 사용합니다.
4. `get_registers`와 `read_memory`로 상태를 조사합니다.
5. `set_breakpoint`, `step`, `resume`으로 실행을 제어합니다.
6. 바이너리 메모리 덤프가 필요하면 `dump_memory` 또는 `dump_region`을 사용합니다.
7. DuckStation을 종료하기 전에 `disconnect`를 호출합니다.

DuckStation의 파일 잠금을 해제해야 한다면 `kill_duckstation`을 호출합니다. 먼저 정상 종료를 요청하고 제한 시간이 지나면 강제 종료합니다. 즉시 강제 종료하려면 `force=true`를 지정합니다.

## 메모리 영역 프리셋

| 이름 | 주소 | 크기 |
| --- | ---: | ---: |
| `ram` | `0x00000000` | 2 MiB |
| `ram_kseg0` | `0x80000000` | 2 MiB |
| `ram_kseg1` | `0xA0000000` | 2 MiB |
| `scratch` | `0x1F800000` | 1 KiB |
| `bios` | `0x1FC00000` | 512 KiB |

`list_memory_regions`로 프리셋을 확인하고 `dump_region`으로 파일에 저장할 수 있습니다.

## 동작 참고사항

- DuckStation은 GDB 클라이언트가 연결될 때 에뮬레이션을 일시 정지합니다. `connect`의 기본값은 `auto_resume=true`이지만 연결 시 짧은 화면 끊김이 발생할 수 있습니다.
- 대용량 메모리 덤프는 16진수 인코딩된 GDB 응답이 소켓 응답 한도를 넘지 않도록 `0x7000`바이트 단위 요청으로 나눕니다.
- 게임 실행 중에도 메모리 읽기 및 쓰기가 동작할 수 있습니다. 안정적인 레지스터 조사와 단일 스텝 작업을 위해서는 게임을 일시 정지하십시오.
- 브레이크포인트에 도달하면 비동기 중지 응답이 생성되며 클라이언트는 다음 MCP 작업에서 해당 응답을 처리합니다.

## 안전 주의사항

이 서버는 에뮬레이션 메모리 수정, 실행 흐름 변경, 임의 경로에 메모리 덤프 저장, DuckStation 프로세스 종료를 수행할 수 있습니다. 로컬 MCP 클라이언트에서만 사용하고 파괴적인 작업을 승인하기 전에 도구 인수를 확인하십시오.

생성된 로그, 덤프, 가상 환경, 바이트코드, 빌드 결과물은 버전 관리에서 제외해야 합니다.

## 라이선스

이 프로젝트에는 [LICENSE](LICENSE)에 명시된 라이선스가 적용됩니다.
