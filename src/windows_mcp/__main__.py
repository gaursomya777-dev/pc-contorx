from windows_mcp.analytics import PostHogAnalytics, with_analytics
from windows_mcp.desktop.service import Desktop,Size
from windows_mcp.watchdog.service import WatchDog
from contextlib import asynccontextmanager
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from typing import Literal, Optional
from fastmcp import FastMCP, Context
from dotenv import load_dotenv
from textwrap import dedent
import pyautogui as pg
import asyncio
import click
import os

load_dotenv()

MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1920, 1080
pg.FAILSAFE=False
pg.PAUSE=0.1

desktop: Optional[Desktop] = None
watchdog: Optional[WatchDog] = None
analytics: Optional[PostHogAnalytics] = None
screen_size:Optional[Size]=None

instructions=dedent(f'''
Windows MCP server provides tools to interact directly with the Windows desktop, 
thus enabling to operate the desktop on the user's behalf.
''')

@asynccontextmanager
async def lifespan(app: FastMCP):
    """Runs initialization code before the server starts and cleanup code after it shuts down."""
    global desktop, watchdog, analytics,screen_size
    
    # Initialize components here instead of at module level
    if os.getenv("ANONYMIZED_TELEMETRY", "true").lower() != "false":
        analytics = PostHogAnalytics()
    desktop = Desktop()
    watchdog = WatchDog()   
    screen_size=desktop.get_screen_size()
    watchdog.set_focus_callback(desktop.tree._on_focus_change)
    
    try:
        watchdog.start()
        await asyncio.sleep(0.1) # Brief pause for watchdog initialization
        yield
    finally:
        if watchdog:
            watchdog.stop()
        if analytics:
            await analytics.close()

mcp=FastMCP(name='windows-mcp',instructions=instructions,lifespan=lifespan)

@mcp.tool(
    name="App",
    description="Manages Windows applications with three modes: 'launch' (opens the prescibed application), 'resize' (adjusts active window size/position), 'switch' (brings specific window into focus).",
    annotations=ToolAnnotations(
        title="App",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "App-Tool")
def app_tool(mode:Literal['launch','resize','switch'],name:str|None=None,window_loc:list[int]|None=None,window_size:list[int]|None=None, ctx: Context = None):
    return desktop.app(mode,name,window_loc,window_size)
    
@mcp.tool(
    name='Shell',
    description='A comprehensive system tool for executing any PowerShell commands. Use it to navigate the file system, manage files and processes, and execute system-level operations. Capable of accessing web content (e.g., via Invoke-WebRequest), interacting with network resources, and performing complex administrative tasks. This tool provides full access to the underlying operating system capabilities, making it the primary interface for system automation, scripting, and deep system interaction.',
    annotations=ToolAnnotations(
        title="Shell",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True
    )
    )
@with_analytics(analytics, "Powershell-Tool")
def powershell_tool(command: str,timeout:int=10, ctx: Context = None) -> str:
    response,status_code=desktop.execute_command(command,timeout)
    return f'Response: {response}\nStatus Code: {status_code}'

@mcp.tool(
    name='Snapshot',
    description='Captures complete desktop state including: system language, focused/opened windows, interactive elements (buttons, text fields, links, menus with coordinates), and scrollable areas. Set use_vision=True to include screenshot. Set use_dom=True for browser content to get web page elements instead of browser UI. Always call this first to understand the current desktop state before taking actions.',
    annotations=ToolAnnotations(
        title="Snapshot",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "State-Tool")
def state_tool(use_vision:bool|str=False,use_dom:bool|str=False, ctx: Context = None):
    use_vision = use_vision is True or (isinstance(use_vision, str) and use_vision.lower() == 'true')
    use_dom = use_dom is True or (isinstance(use_dom, str) and use_dom.lower() == 'true')
    
    # Calculate scale factor to cap resolution at 1080p (1920x1080)
    scale_width = MAX_IMAGE_WIDTH / screen_size.width if screen_size.width > MAX_IMAGE_WIDTH else 1.0
    scale_height = MAX_IMAGE_HEIGHT / screen_size.height if screen_size.height > MAX_IMAGE_HEIGHT else 1.0
    scale = min(scale_width, scale_height)  # Use the smaller scale to ensure both dimensions fit
    
    desktop_state=desktop.get_state(use_vision=use_vision,use_dom=use_dom,as_bytes=True,scale=scale)
    interactive_elements=desktop_state.tree_state.interactive_elements_to_string()
    scrollable_elements=desktop_state.tree_state.scrollable_elements_to_string()
    windows=desktop_state.windows_to_string()
    active_window=desktop_state.active_window_to_string()
    active_desktop=desktop_state.active_desktop_to_string()
    all_desktops=desktop_state.desktops_to_string()
    return [dedent(f'''
    Active Desktop:
    {active_desktop}

    All Desktops:
    {all_desktops}
        
    Focused Window:
    {active_window}

    Opened Windows:
    {windows}

    List of Interactive Elements:
    {interactive_elements or 'No interactive elements found.'}

    List of Scrollable Elements:
    {scrollable_elements or 'No scrollable elements found.'}
    ''')]+([Image(data=desktop_state.screenshot,format='png')] if use_vision else [])

@mcp.tool(
    name='Click',
    description="Performs mouse clicks at specified coordinates [x, y]. Supports button types: 'left' for selection/activation, 'right' for context menus, 'middle'. Supports clicks: 0=hover only (no click), 1=single click (select/focus), 2=double click (open/activate).",
    annotations=ToolAnnotations(
        title="Click",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Click-Tool")
def click_tool(loc:list[int],button:Literal['left','right','middle']='left',clicks:int=1, ctx: Context = None)->str:
    if len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    x,y=loc[0],loc[1]
    desktop.click(loc=loc,button=button,clicks=clicks)
    num_clicks={0:'Hover',1:'Single',2:'Double'}
    return f'{num_clicks.get(clicks)} {button} clicked at ({x},{y}).'

@mcp.tool(
    name='Type',
    description="Types text at specified coordinates [x, y]. Set clear=True to clear existing text first, False to append. Set press_enter=True to submit after typing. Set caret_position to 'start' (beginning), 'end' (end), or 'idle' (default).",
    annotations=ToolAnnotations(
        title="Type",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Type-Tool")
def type_tool(loc:list[int],text:str,clear:bool|str=False,caret_position:Literal['start', 'idle', 'end']='idle',press_enter:bool|str=False, ctx: Context = None)->str:
    if len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    x,y=loc[0],loc[1]
    desktop.type(loc=loc,text=text,caret_position=caret_position,clear=clear,press_enter=press_enter)
    return f'Typed {text} at ({x},{y}).'

@mcp.tool(
    name='Scroll',
    description='Scrolls at coordinates [x, y] or current mouse position if loc=None. Type: vertical (default) or horizontal. Direction: up/down for vertical, left/right for horizontal. wheel_times controls amount (1 wheel ≈ 3-5 lines). Use for navigating long content, lists, and web pages.',
    annotations=ToolAnnotations(
        title="Scroll",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Scroll-Tool")
def scroll_tool(loc:list[int]=None,type:Literal['horizontal','vertical']='vertical',direction:Literal['up','down','left','right']='down',wheel_times:int=1, ctx: Context = None)->str:
    if loc and len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    response=desktop.scroll(loc,type,direction,wheel_times)
    if response:
        return response
    return f'Scrolled {type} {direction} by {wheel_times} wheel times'+f' at ({loc[0]},{loc[1]}).' if loc else ''

@mcp.tool(
    name='Move',
    description='Moves mouse cursor to coordinates [x, y]. Set drag=True to perform a drag-and-drop operation from the current mouse position to the target coordinates. Default (drag=False) is a simple cursor move (hover).',
    annotations=ToolAnnotations(
        title="Move",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Move-Tool")
def move_tool(loc:list[int], drag:bool|str=False, ctx: Context = None)->str:
    drag = drag is True or (isinstance(drag, str) and drag.lower() == 'true')
    if len(loc) != 2:
        raise ValueError("loc must be a list of exactly 2 integers [x, y]")
    x,y=loc[0],loc[1]
    if drag:
        desktop.drag(loc)
        return f'Dragged to ({x},{y}).'
    else:
        desktop.move(loc)
        return f'Moved the mouse pointer to ({x},{y}).'

@mcp.tool(
    name='Shortcut',
    description='Executes keyboard shortcuts using key combinations separated by +. Examples: "ctrl+c" (copy), "ctrl+v" (paste), "alt+tab" (switch apps), "win+r" (Run dialog), "win" (Start menu), "ctrl+shift+esc" (Task Manager). Use for quick actions and system commands.',
    annotations=ToolAnnotations(
        title="Shortcut",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Shortcut-Tool")
def shortcut_tool(shortcut:str, ctx: Context = None):
    desktop.shortcut(shortcut)
    return f"Pressed {shortcut}."

@mcp.tool(
    name='Wait',
    description='Pauses execution for specified duration in seconds. Use when waiting for: applications to launch/load, UI animations to complete, page content to render, dialogs to appear, or between rapid actions. Helps ensure UI is ready before next interaction.',
    annotations=ToolAnnotations(
        title="Wait",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "Wait-Tool")
def wait_tool(duration:int, ctx: Context = None)->str:
    pg.sleep(duration)
    return f'Waited for {duration} seconds.'

@mcp.tool(
    name='Scrape',
    description='Fetch content from a URL or the active browser tab. By default (use_dom=False), performs a lightweight HTTP request to the URL and returns markdown content of complete webpage. Note: Some websites may block automated HTTP requests. If this fails, open the page in a browser and retry with use_dom=True to extract visible text from the active tab\'s DOM within the viewport using the accessibility tree data.',
    annotations=ToolAnnotations(
        title="Scrape",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True
    )
    )
@with_analytics(analytics, "Scrape-Tool")
def scrape_tool(url:str,use_dom:bool|str=False, ctx: Context = None)->str:
    use_dom = use_dom is True or (isinstance(use_dom, str) and use_dom.lower() == 'true')
    if not use_dom:
        content=desktop.scrape(url)
        return f'URL:{url}\nContent:\n{content}'

    desktop_state=desktop.get_state(use_vision=False,use_dom=use_dom)
    tree_state=desktop_state.tree_state
    if not tree_state.dom_node:
        return f'No DOM information found. Please open {url} in browser first.'
    dom_node=tree_state.dom_node
    vertical_scroll_percent=dom_node.vertical_scroll_percent
    content='\n'.join([node.text for node in tree_state.dom_informative_nodes])
    header_status = "Reached top" if vertical_scroll_percent <= 0 else "Scroll up to see more"
    footer_status = "Reached bottom" if vertical_scroll_percent >= 100 else "Scroll down to see more"
    return f'URL:{url}\nContent:\n{header_status}\n{content}\n{footer_status}'

@mcp.tool(
    name='MultiSelect',
    description="Selects multiple items such as files, folders, or checkboxes if press_ctrl=True, or performs multiple clicks if False.",
    annotations=ToolAnnotations(
        title="MultiSelect",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Multi-Select-Tool")
def multi_select_tool(locs:list[list[int]], press_ctrl:bool|str=True, ctx: Context = None)->str:
    press_ctrl = press_ctrl is True or (isinstance(press_ctrl, str) and press_ctrl.lower() == 'true')
    desktop.multi_select(press_ctrl,locs)
    elements_str = '\n'.join([f"({loc[0]},{loc[1]})" for loc in locs])
    return f"Multi-selected elements at:\n{elements_str}"

@mcp.tool(
    name='MultiEdit',
    description="Enters text into multiple input fields at specified coordinates [[x,y,text], ...].",
    annotations=ToolAnnotations(
        title="MultiEdit",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Multi-Edit-Tool")
def multi_edit_tool(locs:list[list], ctx: Context = None)->str:
    desktop.multi_edit(locs)
    elements_str = ', '.join([f"({e[0]},{e[1]}) with text '{e[2]}'" for e in locs])
    return f"Multi-edited elements at: {elements_str}"


# F1: WaitForElement tool
@mcp.tool(
    name='WaitForElement',
    description='Waits for a UI element to appear or disappear from the desktop. Useful for waiting for dialogs, page loads, or spinners. Polls the accessibility tree at a configurable interval until the element is found (or gone) or timeout is reached.',
    annotations=ToolAnnotations(
        title="WaitForElement",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
@with_analytics(analytics, "WaitForElement-Tool")
def wait_for_element_tool(name:str|None=None, control_type:str|None=None,
                          condition:Literal['appear','disappear']='appear',
                          timeout:float=10.0, poll_interval:float=0.3, ctx: Context = None) -> str:
    result = desktop.wait_for_element(name=name, control_type=control_type,
                                       condition=condition, timeout=timeout, poll_interval=poll_interval)
    target = name or control_type or 'element'
    if result:
        return f"Element '{target}' did {condition} within {timeout}s."
    return f"Timeout: Element '{target}' did not {condition} within {timeout}s."

# F2: Filtered Snapshot tool
@mcp.tool(
    name='FilteredSnapshot',
    description='Captures desktop state filtered to a specific window by name. Much faster than full Snapshot when you only need to inspect one window. Falls back to full snapshot if window not found.',
    annotations=ToolAnnotations(
        title="FilteredSnapshot",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
@with_analytics(analytics, "FilteredSnapshot-Tool")
def filtered_snapshot_tool(window_name:str, use_vision:bool|str=False, use_dom:bool|str=False, ctx: Context = None):
    use_vision = use_vision is True or (isinstance(use_vision, str) and use_vision.lower() == 'true')
    use_dom = use_dom is True or (isinstance(use_dom, str) and use_dom.lower() == 'true')

    scale_width = MAX_IMAGE_WIDTH / screen_size.width if screen_size.width > MAX_IMAGE_WIDTH else 1.0
    scale_height = MAX_IMAGE_HEIGHT / screen_size.height if screen_size.height > MAX_IMAGE_HEIGHT else 1.0
    scale = min(scale_width, scale_height)

    desktop_state = desktop.get_filtered_state(window_name=window_name, use_vision=use_vision, use_dom=use_dom, as_bytes=True, scale=scale)
    interactive_elements = desktop_state.tree_state.interactive_elements_to_string()
    scrollable_elements = desktop_state.tree_state.scrollable_elements_to_string()
    windows = desktop_state.windows_to_string()
    active_window = desktop_state.active_window_to_string()

    return [dedent(f'''
    Filtered by window: {window_name}

    Focused Window:
    {active_window}

    Opened Windows:
    {windows}

    List of Interactive Elements:
    {interactive_elements or 'No interactive elements found.'}

    List of Scrollable Elements:
    {scrollable_elements or 'No scrollable elements found.'}
    ''')] + ([Image(data=desktop_state.screenshot, format='png')] if use_vision else [])

# F3: Clipboard tool
@mcp.tool(
    name='Clipboard',
    description="Reads or writes the system clipboard. Mode 'read' returns current clipboard content (text or image). Mode 'write' sets clipboard text content.",
    annotations=ToolAnnotations(
        title="Clipboard",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Clipboard-Tool")
def clipboard_tool(mode:Literal['read','write'], content:str|None=None,
                   format:Literal['text','image']='text', ctx: Context = None):
    if mode == 'read':
        result = desktop.clipboard_read(format=format)
        if format == 'image' and result:
            return [Image(data=result, format='png')]
        return f"Clipboard content: {result}" if result else "Clipboard is empty."
    elif mode == 'write':
        if content is None:
            return "Error: content is required for write mode."
        desktop.clipboard_write(content)
        return f"Clipboard set to: {content[:100]}{'...' if len(content) > 100 else ''}"

# F4: DragFile tool
@mcp.tool(
    name='DragFile',
    description='Drags a file from the file system to a target location on screen. Copies the file path to clipboard as CF_HDROP and pastes at the target coordinates.',
    annotations=ToolAnnotations(
        title="DragFile",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "DragFile-Tool")
def drag_file_tool(file_path:str, target_x:int, target_y:int, ctx: Context = None) -> str:
    try:
        desktop.drag_file(file_path, target_x, target_y)
        return f"File '{file_path}' dropped at ({target_x},{target_y})."
    except FileNotFoundError as e:
        return str(e)

# F5: OCR tool
@mcp.tool(
    name='OCR',
    description='Extracts text from the current screen using OCR (Optical Character Recognition). Useful when accessibility tree returns no elements (custom UIs, games, PDF viewers). Returns text with bounding box coordinates. Requires pytesseract to be installed.',
    annotations=ToolAnnotations(
        title="OCR",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
@with_analytics(analytics, "OCR-Tool")
def ocr_tool(ctx: Context = None) -> str:
    results = desktop.ocr_screenshot()
    if not results:
        return "No text detected via OCR. Ensure pytesseract is installed."
    lines = ["# OCR Results", "# text|coords|confidence"]
    for r in results:
        lines.append(f"{r['text']}|({r['center'].x},{r['center'].y})|{r['confidence']}%")
    return "\n".join(lines)

# F6: Batch execution tool
@mcp.tool(
    name='Batch',
    description='Executes multiple actions in a single call to reduce round-trips. Accepts a list of actions, each with a tool name and parameters. Actions are executed sequentially. Supported tools: Click, Type, Scroll, Move, Shortcut, Wait.',
    annotations=ToolAnnotations(
        title="Batch",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Batch-Tool")
def batch_tool(actions:list[dict], ctx: Context = None) -> str:
    results = []
    tool_map = {
        'click': lambda p: (desktop.click(loc=p['loc'], button=p.get('button', 'left'), clicks=p.get('clicks', 1)), f"Clicked at {p['loc']}"),
        'type': lambda p: (desktop.type(loc=p['loc'], text=p['text'], caret_position=p.get('caret_position', 'idle'), clear=p.get('clear', False), press_enter=p.get('press_enter', False)), f"Typed '{p['text']}' at {p['loc']}"),
        'scroll': lambda p: (desktop.scroll(loc=p.get('loc'), type=p.get('type', 'vertical'), direction=p.get('direction', 'down'), wheel_times=p.get('wheel_times', 1)), f"Scrolled {p.get('direction', 'down')}"),
        'move': lambda p: (desktop.move(loc=p['loc']), f"Moved to {p['loc']}"),
        'shortcut': lambda p: (desktop.shortcut(p['shortcut']), f"Pressed {p['shortcut']}"),
        'wait': lambda p: (pg.sleep(p.get('duration', 1)), f"Waited {p.get('duration', 1)}s"),
    }

    for i, action in enumerate(actions):
        tool_name = action.get('tool', '').lower()
        params = action.get('params', {})
        try:
            if tool_name in tool_map:
                _, msg = tool_map[tool_name](params)
                results.append(f"[{i}] OK: {msg}")
            else:
                results.append(f"[{i}] ERROR: Unknown tool '{tool_name}'")
        except Exception as e:
            results.append(f"[{i}] ERROR: {e}")
    return "\n".join(results)

# F7: Highlight tool
@mcp.tool(
    name='Highlight',
    description='Draws a temporary colored rectangle overlay on a target area for visual debugging. Useful for confirming which element the agent is targeting before clicking.',
    annotations=ToolAnnotations(
        title="Highlight",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Highlight-Tool")
def highlight_tool(x:int, y:int, width:int, height:int,
                   duration:float=2.0, color:str='red', ctx: Context = None) -> str:
    desktop.highlight_element(x, y, width, height, duration, color)
    return f"Highlighting ({x},{y}) {width}x{height} in {color} for {duration}s."

# F8: GetNotifications tool
@mcp.tool(
    name='GetNotifications',
    description='Retrieves recent Windows notifications/toasts from the system. Checks the notification area and Action Center for visible notifications.',
    annotations=ToolAnnotations(
        title="GetNotifications",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
@with_analytics(analytics, "GetNotifications-Tool")
def get_notifications_tool(ctx: Context = None) -> str:
    notifications = desktop.get_notifications()
    if not notifications:
        return "No notifications found."
    lines = ["# Recent Notifications"]
    for i, n in enumerate(notifications):
        lines.append(f"{i}. [{n.get('control_type', 'Unknown')}] {n.get('text', '')}")
    return "\n".join(lines)


@click.command()
@click.option(
    "--transport",
    help="The transport layer used by the MCP server.",
    type=click.Choice(['stdio','sse','streamable-http']),
    default='stdio'
)
@click.option(
    "--host",
    help="Host to bind the SSE/Streamable HTTP server.",
    default="localhost",
    type=str,
    show_default=True
)
@click.option(
    "--port",
    help="Port to bind the SSE/Streamable HTTP server.",
    default=8000,
    type=int,
    show_default=True
)
def main(transport, host, port):
    match transport:
        case 'stdio':
            mcp.run(transport=transport,show_banner=False)
        case 'sse'|'streamable-http':
            mcp.run(transport=transport,host=host,port=port,show_banner=False)
        case _:
            raise ValueError(f"Invalid transport: {transport}")

if __name__ == "__main__":
    main()
