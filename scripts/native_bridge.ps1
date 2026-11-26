param([Parameter(Mandatory=$true)][string]$RequestFile)
$ErrorActionPreference='Stop'
$OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]
$S=[System.Windows.Automation.TreeScope]
$W=[System.Windows.Automation.TreeWalker]::RawViewWalker
$r=Get-Content -LiteralPath $RequestFile -Raw -Encoding utf8 | ConvertFrom-Json
$p=$r.config.profile

function Emit($value){$value|ConvertTo-Json -Depth 30 -Compress}
function Fail([string]$code){throw [InvalidOperationException]::new($code)}
function ClassNodes($root,[string]$class){
 if(!$class){Fail 'native_selector_not_configured'}
 $condition=[System.Windows.Automation.PropertyCondition]::new($A::ClassNameProperty,$class)
 return @($root.FindAll($S::Descendants,$condition)|Where-Object {!$_.Current.IsOffscreen})
}
function One($root,[string]$class){
 $nodes=@(ClassNodes $root $class)
 if($nodes.Count -ne 1){Fail 'native_control_not_unique'}
 return $nodes[0]
}
function Id($element){
 $id=$element.Current.AutomationId
 if(!$id){Fail 'stable_automation_id_unavailable'}
 return $id
}
function Text($element){
 $v=$null
 if($element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$v)){
  return [string]$v.Current.Value
 }
 $v=$null
 if($element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern,[ref]$v)){
  return [string]$v.DocumentRange.GetText(-1)
 }
 return [string]$element.Current.Name
}
function Classes($element){return @(([string]$element.Current.ClassName) -split '\s+'|Where-Object {$_})}
function MessageSignature($messages){
 return (@($messages|ForEach-Object {ConvertTo-Json -InputObject @($_.id,$_.role,$_.text,$_.kind) -Compress}) -join "`n")
}
function DocumentUrl($element){
 $v=$null
 if($element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$v)){return [string]$v.Current.Value}
 return ''
}
function Docs($root){
 $condition=[System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document)
 return @($root.FindAll($S::Descendants,$condition))
}
function BoundRoot {
 $b=$r.config.binding
 if(!$b){Fail 'native_binding_required'}
 $proc=Get-Process -Id $b.pid
 if($proc.ProcessName -notin @('Codex','ChatGPT') -or $proc.StartTime.ToUniversalTime().Ticks.ToString() -ne $b.birth){Fail 'bound_process_changed'}
 $hostRoot=$A::FromHandle([IntPtr][long]$b.hwnd)
 if($hostRoot.Current.ProcessId -ne $b.pid){Fail 'bound_window_changed'}
 $documents=@(Docs $hostRoot)
 $chats=@($documents|Where-Object {!$_.Current.IsOffscreen -and $_.Current.Name -eq $b.chat_title -and (DocumentUrl $_) -like 'app://*/index.html*'})
 if($chats.Count -ne 1){Fail 'bound_chat_not_visible'}
 $matches=@($documents|Where-Object {!$_.Current.IsOffscreen -and ($_.GetRuntimeId() -join ',') -eq $b.runtime_id -and (DocumentUrl $_) -match '^https://www\.zhipin\.com/web/'})
 if($matches.Count -ne 1){Fail 'bound_boss_document_lost'}
 return $matches[0]
}
function Invoke($element){
 $pattern=$null
 if($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){$pattern.Invoke();return}
 $pattern=$null
 if($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)){$pattern.Select();return}
 Fail 'native_action_pattern_unavailable'
}
function ContactRows($root){
 $list=One $root $p.list_class
 return @(ClassNodes $list $p.item_class)
}
function Selected($root){
 $rows=@(ContactRows $root|Where-Object {(Classes $_) -contains $p.selected_class})
 if($rows.Count -ne 1){Fail 'selected_contact_not_unique'}
 return $rows[0]
}
function GuardDraft($root){
 $edit=One $root $p.editor_class
 if((Text $edit).Trim()){Fail 'human_draft_present'}
}
function Snapshot($root){
 $selected=Selected $root
 $cid=Id $selected
 $contactName=(Text (One $selected $p.contact_name_class)).Trim()
 $headerName=(Text (One $root $p.name_class)).Trim()
 if(!$contactName -or $contactName -ne $headerName){Fail 'conversation_header_mismatch'}
 $jobTitle=(Text (One $root $p.job_class)).Trim()
 $projectProperty=$r.config.job_aliases.PSObject.Properties[$jobTitle]
 if(!$projectProperty -or $projectProperty.Value -notin $r.config.allowed_jobs){Fail 'job_not_explicitly_mapped'}
 $messages=@()
 $scope=One $root $p.message_root_class
 $messageScroll=$null
 if($scope.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern,[ref]$messageScroll) -and $messageScroll.Current.VerticallyScrollable -and $messageScroll.Current.VerticalScrollPercent -lt 99.9){Fail 'message_view_not_at_latest'}
 $seen=[Collections.Generic.HashSet[string]]::new()
 foreach($row in @(ClassNodes $scope $p.message_class)){
  $id=Id $row
  if(!$seen.Add($id)){Fail 'message_ids_not_unique'}
  $tokens=@(Classes $row)
  $child=$W.GetFirstChild($row)
  while($child){$tokens+=@(Classes $child);$child=$W.GetNextSibling($child)}
  $incoming=@($tokens|Where-Object {$_ -in $p.incoming_classes}).Count -gt 0
  $outgoing=@($tokens|Where-Object {$_ -in $p.outgoing_classes}).Count -gt 0
  $system=@($tokens|Where-Object {$_ -in $p.system_classes}).Count -gt 0
  if($system){$role='system'}
  elseif($incoming -and !$outgoing){$role='candidate'}
  elseif($outgoing -and !$incoming){$role='recruiter'}
  else{Fail 'message_direction_unknown'}
  $content=One $row $p.message_text_class
  $text=(Text $content).Trim()
  if(!$text -or $text.Length -gt 4000){Fail 'message_text_unavailable_or_too_long'}
  $messages+=@{id=$id;role=$role;text=$text;kind='text'}
 }
 if(!$messages.Count){Fail 'conversation_has_no_readable_messages'}
 $editor=One $root $p.editor_class
 return @{conversation_id=$cid;job_id=[string]$projectProperty.Value;job_title=$jobTitle;messages=$messages;draft=(Text $editor);identity_verified=$true;complete=$true}
}
function OpenConversation([string]$cid){
 $root=BoundRoot

 GuardDraft $root
 $list=One $root $p.list_class
 $scroll=$null
 $canScroll=$list.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern,[ref]$scroll) -and $scroll.Current.VerticallyScrollable
 if($canScroll){$scroll.SetScrollPercent(-1,0)}
 $found=$null
 for($page=0;$page -lt 30;$page++){
  $items=@(ContactRows $root|Where-Object {$_.Current.AutomationId -eq $cid})
  if($items.Count -gt 1){Fail 'contact_id_not_unique'}
  if($items.Count -eq 1){$found=$items[0];break}
  if(!$canScroll -or $scroll.Current.VerticalScrollPercent -ge 99.9){break}
  $before=$scroll.Current.VerticalScrollPercent
  $scroll.Scroll([System.Windows.Automation.ScrollAmount]::NoAmount,[System.Windows.Automation.ScrollAmount]::LargeIncrement)
  Start-Sleep -Milliseconds 180
  if($scroll.Current.VerticalScrollPercent -eq $before){Fail 'contact_scroll_stalled'}
 }
 if(!$found){Fail 'contact_not_found'}
 $selected=$null
 try{$selected=Selected $root}catch{}
 if(!$selected -or (Id $selected) -ne $cid){Invoke $found;Start-Sleep -Milliseconds 500}
 $lastError=$null
 for($attempt=0;$attempt -lt 8;$attempt++){
  try{
   $snapshot=Snapshot (BoundRoot)
   if($snapshot.conversation_id -ne $cid){Fail 'selected_conversation_changed'}
   return $snapshot
  }catch{$lastError=$_;Start-Sleep -Milliseconds 200}
 }
 throw $lastError
}

try{
 if($r.action -eq 'binding_status'){
  $b=$r.config.binding
  $hostRoot=$A::FromHandle([IntPtr][long]$b.hwnd)
  $items=@(Docs $hostRoot|ForEach-Object {@{name=$_.Current.Name;url=(DocumentUrl $_);offscreen=$_.Current.IsOffscreen;runtime=($_.GetRuntimeId() -join ',')}})
  Emit @{documents=$items};exit
 }
 if($r.action -eq 'bind'){
  $hostRoot=$A::FromHandle([IntPtr][long]$r.window)
  $proc=Get-Process -Id $hostRoot.Current.ProcessId
  if($proc.ProcessName -notin @('Codex','ChatGPT')){Fail 'codex_window_required'}
  $documents=@(Docs $hostRoot)
  $chat=@($documents|Where-Object {!$_.Current.IsOffscreen -and $_.Current.Name -eq $r.chat_title -and (DocumentUrl $_) -like 'app://*/index.html*'})
  $boss=@($documents|Where-Object {!$_.Current.IsOffscreen -and (DocumentUrl $_) -match '^https://www\.zhipin\.com/web/chat/'})
  if($chat.Count -ne 1 -or $boss.Count -ne 1){Fail 'unique_current_chat_and_boss_chat_document_required'}
  Emit @{binding=@{hwnd=[long]$r.window;pid=$proc.Id;birth=$proc.StartTime.ToUniversalTime().Ticks.ToString();chat_title=$r.chat_title;runtime_id=($boss[0].GetRuntimeId() -join ',')}}
  exit
 }
 $root=BoundRoot
 if($r.action -eq 'inspect'){
  $filter=if($r.filter){[string]$r.filter}else{'chat|message|user-item|friend-item|editor|job|select|position'}
  $hints=@()
  foreach($node in $root.FindAll($S::Descendants,[System.Windows.Automation.Condition]::TrueCondition)){
   if($node.Current.ClassName -notmatch $filter){continue}
   $patterns=@($node.GetSupportedPatterns()|ForEach-Object {$_.ProgrammaticName})
   $parent=$W.GetParent($node)
   $legacy=$null;$legacyValue='';$defaultAction=''

   $hints+=@{class=$node.Current.ClassName;name=$node.Current.Name;id=$node.Current.AutomationId;runtime=($node.GetRuntimeId() -join ',');offscreen=$node.Current.IsOffscreen;patterns=$patterns;parent_class=$parent.Current.ClassName;legacy_value=$legacyValue;default_action=$defaultAction}
   if($hints.Count -ge 100){break}
  }
  Emit @{state='inspect';hints=$hints};exit
 }
 if($r.action -eq 'probe'){
  # Structure only, never a dump of candidate message text or the whole desktop.
  $classes=@($root.FindAll($S::Descendants,[System.Windows.Automation.Condition]::TrueCondition)|Where-Object {$_.Current.ClassName -match 'chat|message|user-item|friend-item|editor|job-name|geek-name'})
  $hints=@($classes|Select-Object -First 120|ForEach-Object {@{class=$_.Current.ClassName;type=$_.Current.ControlType.ProgrammaticName;has_automation_id=[bool]$_.Current.AutomationId;offscreen=$_.Current.IsOffscreen}})
  Emit @{state='probe';hints=$hints;profile_verified=[bool]$p.verified};exit
 }
 if(!$p.verified -or !$p.stable_ids_verified){Fail 'native_calibration_required'}
 if($r.action -eq 'scan'){
  GuardDraft $root
  $list=One $root $p.list_class
  $scroll=$null
  $canScroll=$list.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern,[ref]$scroll) -and $scroll.Current.VerticallyScrollable
  if($canScroll){$scroll.SetScrollPercent(-1,0);Start-Sleep -Milliseconds 150}
  $ids=[Collections.Generic.HashSet[string]]::new()
  $unread=[Collections.Generic.HashSet[string]]::new()
  $complete=$false
  for($page=0;$page -lt 30;$page++){
   foreach($item in @(ContactRows $root)){
    $cid=Id $item
    $null=$ids.Add($cid)
    if(@(ClassNodes $item $p.unread_class).Count -gt 0){$null=$unread.Add($cid)}
   }
   if(!$canScroll -or $scroll.Current.VerticalScrollPercent -ge 99.9){$complete=$true;break}
   $before=$scroll.Current.VerticalScrollPercent
   $scroll.Scroll([System.Windows.Automation.ScrollAmount]::NoAmount,[System.Windows.Automation.ScrollAmount]::LargeIncrement)
   Start-Sleep -Milliseconds 180
   if($scroll.Current.VerticalScrollPercent -eq $before){Fail 'contact_scroll_stalled'}
  }
  # An open conversation may receive messages without an unread badge.
  try{$active=Selected $root;$null=$unread.Add((Id $active))}catch{}
  Emit @{complete=$complete;conversation_ids=@($ids|Sort-Object);unread_ids=@($unread|Sort-Object)};exit
 }
 if($r.action -eq 'read'){Emit @{snapshot=(OpenConversation $r.conversation_id)};exit}
 if($r.action -eq 'send'){
  if(!$p.send_verified -or !$p.send_name){Fail 'native_send_calibration_required'}
  $before=Snapshot (BoundRoot)
  $expected=$r.expected
  if($before.conversation_id -ne $expected.conversation_id -or $before.job_id -ne $expected.job_id -or $before.job_title -ne $expected.job_title -or $before.draft.Trim() -or (MessageSignature $before.messages) -ne (MessageSignature $expected.messages)){Fail 'pre_send_context_changed'}
  $editor=One $root $p.editor_class
  $pattern=$null
  if(!$editor.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$pattern) -or $pattern.Current.IsReadOnly){Fail 'editor_value_pattern_unavailable'}
  if(!$r.text -or $r.text.Length -gt 1000){Fail 'reply_text_invalid'}
  $pattern.SetValue([string]$r.text)
  $afterFill=Snapshot (BoundRoot)
  if($afterFill.draft -ne $r.text -or $afterFill.conversation_id -ne $before.conversation_id -or $afterFill.job_id -ne $before.job_id -or $afterFill.job_title -ne $before.job_title -or (MessageSignature $afterFill.messages) -ne (MessageSignature $before.messages)){Fail 'context_changed_after_fill'}
  $buttonCondition=[System.Windows.Automation.AndCondition]::new([System.Windows.Automation.Condition[]]@(
   [System.Windows.Automation.PropertyCondition]::new($A::NameProperty,[string]$p.send_name),
   [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)))
  $buttons=@($root.FindAll($S::Descendants,$buttonCondition)|Where-Object {!$_.Current.IsOffscreen -and $_.Current.IsEnabled})
  if($buttons.Count -ne 1){Fail 'send_button_not_unique'}
  Invoke $buttons[0]
  $oldIds=@($before.messages|ForEach-Object {$_.id})
  for($attempt=0;$attempt -lt 12;$attempt++){
   Start-Sleep -Milliseconds 250
   $after=Snapshot (BoundRoot)
   if($after.conversation_id -ne $before.conversation_id -or $after.job_id -ne $before.job_id -or $after.job_title -ne $before.job_title){Fail 'conversation_changed_during_verification'}
   $sent=@($after.messages|Where-Object {$_.role -eq 'recruiter' -and $_.text -eq $r.text -and $_.id -notin $oldIds})
   if($sent.Count -eq 1 -and !$after.draft.Trim()){
    Emit @{state='sent';conversation_id=$after.conversation_id;job_id=$after.job_id;text=[string]$r.text;message_id=$sent[0].id};exit
   }
  }
  Fail 'send_result_unknown'
 }
 Fail 'unknown_native_action'
}catch{
 $message=$_.Exception.Message
 if($message -notmatch '^[a-z][a-z0-9_]{2,100}$'){$message='native_bridge_exception'}
 Emit @{error=$message}
 exit 1
}
