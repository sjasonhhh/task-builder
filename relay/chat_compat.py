"""Per-client Messages <-> Chat Completions adapter for relay exec.

The shared relay and its credentials stay untouched. This loopback server lives
only as long as the client, and forwards to the existing authenticated relay.
Text and client tool calls are supported. Unsupported content fails explicitly.
Upstream replies are buffered; SSE pings keep long requests alive, then the
complete reply is emitted using the documented Anthropic event sequence.
"""
from __future__ import annotations
import hmac
import json
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def text_content(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    parts=[]
    for block in content:
        if block.get('type')!='text':
            raise ValueError('Chat adapter does not support content block: '+str(block.get('type')))
        parts.append(block['text'])
    return '\n'.join(parts)


def to_chat(body, model, reasoning_cache=None):
    messages=[]
    if body.get('system'):
        messages.append({'role':'system','content':text_content(body['system'])})
    for message in body['messages']:
        role=message['role'];content=message['content']
        if isinstance(content,str):
            messages.append({'role':role,'content':content});continue
        if role=='assistant':
            text=[];calls=[];reason=[]
            for block in content:
                kind=block['type']
                if kind=='text':text.append(block['text'])
                elif kind=='tool_use':
                    calls.append({'id':block['id'],'type':'function','function':{'name':block['name'],'arguments':json.dumps(block['input'],ensure_ascii=False)}})
                elif kind=='thinking':reason.append(block.get('thinking',''))
                else:raise ValueError('Unsupported assistant block: '+str(kind))
            converted={'role':'assistant','content':'\n'.join(text) or None}
            if calls:
                converted['tool_calls']=calls
                cached=(reasoning_cache or {}).get(calls[0]['id'])
                if cached:converted['reasoning_content']=cached
            if reason:converted['reasoning_content']='\n'.join(reason)
            messages.append(converted)
        elif role=='user':
            text=[]
            for block in content:
                if block['type']=='tool_result':
                    # Tool replies must precede any new user text in this turn.
                    result=text_content(block.get('content',''))
                    if block.get('is_error'):result='Tool error: '+result
                    messages.append({'role':'tool','tool_call_id':block['tool_use_id'],'content':result})
                elif block['type']=='text':text.append(block['text'])
                else:raise ValueError('Unsupported user block: '+str(block['type']))
            if text:messages.append({'role':'user','content':'\n'.join(text)})
        else:raise ValueError('Unsupported role: '+str(role))
    result={'model':model,'messages':messages,'stream':False}
    for name in ('max_tokens','temperature','top_p'):
        if name in body:result[name]=body[name]
    if body.get('stop_sequences'):result['stop']=body['stop_sequences']
    if body.get('tools'):
        tools=[]
        for tool in body['tools']:
            if not tool.get('name') or 'input_schema' not in tool:
                raise ValueError('Only client tools with input_schema are supported')
            tools.append({'type':'function','function':{'name':tool['name'],'description':tool.get('description',''),'parameters':tool['input_schema']}})
        result['tools']=tools
    choice=body.get('tool_choice',{})
    if choice:
        mode=choice.get('type')
        if mode=='tool':result['tool_choice']={'type':'function','function':{'name':choice['name']}}
        elif mode in ('auto','none','any'):result['tool_choice']='required' if mode=='any' else mode
        else:raise ValueError('Unsupported tool_choice')
        if choice.get('disable_parallel_tool_use'):result['parallel_tool_calls']=False
    return result


def to_message(reply, model, reasoning_cache=None):
    choice=reply['choices'][0];message=choice['message'];content=[]
    if message.get('content'):content.append({'type':'text','text':text_content(message['content'])})
    if message.get('refusal'):content.append({'type':'text','text':message['refusal']})
    for call in message.get('tool_calls',[]):
        function=call['function'];args=json.loads(function['arguments'])
        if not isinstance(args,dict):raise ValueError('Tool arguments must be an object')
        content.append({'type':'tool_use','id':call['id'],'name':function['name'],'input':args})
        if reasoning_cache is not None and message.get('reasoning_content'):
            reasoning_cache[call['id']]=message['reasoning_content']
    if reasoning_cache is not None:
        while len(reasoning_cache)>256:reasoning_cache.pop(next(iter(reasoning_cache)))
    finish=choice.get('finish_reason')
    if finish not in ('stop','length','tool_calls','content_filter'):
        raise ValueError('Unknown upstream finish_reason: '+str(finish))
    reason={'stop':'end_turn','length':'max_tokens','tool_calls':'tool_use','content_filter':'end_turn'}[finish]
    if finish=='content_filter' and not content:
        content=[{'type':'text','text':'The upstream service declined this request.'}]
    usage=reply.get('usage',{});cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
    return {'id':reply.get('id') or 'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant',
            'model':model,'content':content,'stop_reason':reason,'stop_sequence':None,
            'usage':{'input_tokens':max(0,usage.get('prompt_tokens',0)-cached),
                     'output_tokens':usage.get('completion_tokens',0),
                     'cache_read_input_tokens':cached,'cache_creation_input_tokens':0}}


def message_events(message):
    yield {'type':'message_start','message':{**message,'content':[],'stop_reason':None,'usage':{**message['usage'],'output_tokens':0}}}
    for index,block in enumerate(message['content']):
        if block['type']=='text':
            start={'type':'text','text':''};delta={'type':'text_delta','text':block['text']}
        else:
            start={**block,'input':{}};delta={'type':'input_json_delta','partial_json':json.dumps(block['input'],ensure_ascii=False)}
        yield {'type':'content_block_start','index':index,'content_block':start}
        yield {'type':'content_block_delta','index':index,'delta':delta}
        yield {'type':'content_block_stop','index':index}
    yield {'type':'message_delta','delta':{'stop_reason':message['stop_reason'],'stop_sequence':None},'usage':{'output_tokens':message['usage']['output_tokens']}}
    yield {'type':'message_stop'}


class ChatBridge:
    def __init__(self, entry, debug_dir=None):
        self.entry=entry
        self.server=None
        self.reasoning_cache={}
        self.debug_dir=Path(debug_dir) if debug_dir else None

    def diagnostic(self, suffix, value):
        if self.debug_dir is None:
            return
        # Opt-in diagnostics contain API bodies only, never authentication headers.
        self.debug_dir.mkdir(parents=True,exist_ok=True)
        path=self.debug_dir/(uuid.uuid4().hex+'-'+suffix+'.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as handle:
            json.dump(value,handle,ensure_ascii=False)

    def start(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args):pass
            def send_json(self,status,value):
                blob=json.dumps(value,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob)
            def event(self,event):
                self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
            def do_POST(self):
                supplied=self.headers.get('x-api-key','')
                auth=self.headers.get('Authorization','')
                if auth.lower().startswith('bearer '):supplied=auth[7:]
                if not hmac.compare_digest(supplied,owner.entry['token']):
                    self.send_json(401,{'type':'error','error':{'type':'authentication_error','message':'Invalid relay token'}});return
                if urlsplit(self.path).path.rstrip('/')!='/v1/messages':
                    self.send_json(501,{'type':'error','error':{'type':'invalid_request_error','message':'This adapter only supports /v1/messages; token counting is not implemented'}});return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=128*1024*1024:raise ValueError('Invalid body size')
                    body=json.loads(self.rfile.read(length))
                    request=to_chat(body,owner.entry['model'],owner.reasoning_cache)
                    owner.diagnostic('chat-request',request)
                except (KeyError,TypeError,ValueError) as e:
                    self.send_json(400,{'type':'error','error':{'type':'invalid_request_error','message':str(e)}});return
                result=queue.Queue(maxsize=1)
                def fetch():
                    try:
                        req=Request(owner.entry['base_url'].rstrip('/')+'/v1/chat/completions',data=json.dumps(request,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+owner.entry['token'],'Content-Type':'application/json'})
                        with build_opener(ProxyHandler({})).open(req,timeout=600) as response:
                            message=to_message(json.load(response),owner.entry['model'],owner.reasoning_cache)
                        result.put((200,message))
                    except HTTPError as e:
                        try:upstream=json.loads(e.read(65536));detail=upstream.get('error',upstream)
                        except ValueError:detail={'message':'Upstream HTTP '+str(e.code)}
                        if not isinstance(detail,dict):detail={'message':str(detail)}
                        owner.diagnostic('upstream-error',{'status':e.code,'error':detail})
                        error_type={401:'authentication_error',403:'permission_error',429:'rate_limit_error'}.get(e.code,'api_error')
                        result.put((e.code,{'type':'error','error':{'type':error_type,'message':str(detail.get('message','Upstream error'))}}))
                    except Exception as e:
                        result.put((502,{'type':'error','error':{'type':'api_error','message':'Chat conversion or connection failed: '+type(e).__name__}}))
                threading.Thread(target=fetch,daemon=True).start()
                streaming=bool(body.get('stream'));started=False
                try:
                    while True:
                        try:status,value=result.get(timeout=10);break
                        except queue.Empty:
                            if streaming:
                                if not started:
                                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();started=True
                                self.event({'type':'ping'})
                    if not streaming or status!=200 and not started:
                        self.send_json(status,value);return
                    if not started:
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                    if status!=200:self.event(value)
                    else:
                        for event in message_events(value):self.event(event)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return 'http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        if self.server:
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)


def run_client(entry,program,env):
    bridge=ChatBridge(entry,env.get('RELAY_CHAT_DEBUG_DIR'))
    environment=dict(env)
    environment['ANTHROPIC_BASE_URL']=bridge.start()
    environment['ENABLE_TOOL_SEARCH']='false'
    try:
        process=subprocess.Popen(program,env=environment)
        try:return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:return process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();return process.wait()
    finally:bridge.close()
