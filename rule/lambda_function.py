import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import subprocess
import logging

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors

eh = ExtensionHandler()

# Import your clients
client = boto3.client('events')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

"""
eh calls
    eh.add_op() call MUST be made for the function to execute! Adds functions to the execution queue.
    eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
    eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
    eh.retry_error(a_unique_id_for_the_error(if you don't want it to fail out after 6 tries), progress=65, callback_sec=8)
        This is how to wait and try again
        Only set callback seconds for a wait, not an error
        @ext() runs the function if its operation is present and there isn't already a retry declared
    eh.add_log() is how logs are passed to the front-end
    eh.perm_error() is how you permanently fail the component deployment and send a useful message to the front-end
    eh.finish() just finishes the deployment and sends back message and progress
    *RARE*
    eh.add_state() takes a dictionary, merges existing with new
        This is specifically if CloudKommand doesn't need to store it for later. Thrown away at the end of the deployment.
        Preserved across retries, but not across deployments.
There are three elements of state preserved across retries:
    - eh.props
    - eh.links 
    - eh.state 
Wrap all operations you want to run with the following:
    @ext(handler=eh, op="your_operation_name")
Progress only needs to be explicitly reported on 1) a retry 2) an error. Finishing auto-sets progress to 100. 
"""

def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default
    

def lambda_handler(event, context):
    try:
        # All relevant data is generally in the event, excepting the region and account number
        print(f"event = {event}")
        region = account_context(context)['region']
        account_number = account_context(context)['number']

        # This copies the operations, props, links, retry data, and remaining operations that are sent from CloudKommand. 
        # Just always include this.
        eh.capture_event(event)

        # These are other important values you will almost always use
        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")

        # you pull in whatever arguments you care about
        """
        # Some examples. S3 doesn't really need any because each attribute is a separate call. 
        auto_verified_attributes = cdef.get("auto_verified_attributes") or ["email"]
        alias_attributes = cdef.get("alias_attributes") or ["preferred_username", "phone_number", "email"]
        username_attributes = cdef.get("username_attributes") or None
        """

        name = prev_state.get("props", {}).get("name") or cdef.get("name") or component_safe_name(project_code, repo_id, cname, no_underscores=False, no_uppercase=False, max_chars=63)
        eh.add_state({"name": name})

        ### ATTRIBUTES THAT CAN BE SET ON INITIAL CREATION
        schedule_expression = cdef.get("schedule_expression")
        event_pattern = cdef.get("event_pattern")
        state = 'ENABLED' # Maybe in the future we allow this to be set by the user.
        description = cdef.get("description") or None
        role_arn = cdef.get("role_arn") or None
        tags = cdef.get('tags') # this is converted to a [{"Key": key, "Value": value} , ...] format

        targets = cdef.get("targets") or []

        # remove any None values from the attributes dictionary        
        attributes = remove_none_attributes({
            "Name": str(name) if name else name,
            "ScheduleExpression": schedule_expression,
            "EventPattern": json.dumps(event_pattern) if event_pattern else None,
            "State": state,
            "Description": str(description) if description else description,
            "RoleArn": role_arn,
            "Tags": [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()] if tags else None,
            "Targets": targets
        })

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_name = None

            try:
                old_name = prev_state["props"]["name"]
            except:
                pass

            eh.add_op("get_rule")

            # If any non-editable fields have changed, we are choosing to fail. 
            # We are NOT choosing to delete and recreate the rule
            if (old_name and old_name != name):

                non_editable_error_message = "You may not edit the name of the existing rule. Please create a new component with the desired name."
                eh.add_log("Cannot edit non-editable field", {"error": non_editable_error_message}, is_error=True)
                eh.perm_error(non_editable_error_message, 10)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_rule")
            eh.add_state({"name": prev_state["props"]["name"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_rule(attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_rule()

        ### CREATE CALL(S) (occasionally multiple)
        create_rule(attributes, region, prev_state)
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        update_rule(attributes, region, prev_state)
        remove_tags()
        set_tags()
        remove_targets()
        put_targets()

        add_permissions_for_targets()
        add_lambda_permissions(account_number)
        add_sns_permissions()
        add_sqs_permissions(account_number)
        ### GENERATE PROPS (sometimes can be done in get/create)

        # IMPORTANT! ALWAYS include this. Sends back appropriate data to CloudKommand.
        return eh.finish()

    # You want this. Leave it.
    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

### GET STATE
# ALWAYS put the ext decorator on ALL calls that are referenced above
# This is ONLY called when this operation is slated to occur.
# GENERALLY, this function will make a bunch of eh.add_op() calls which determine what actions will be executed.
#   The eh.add_op() call MUST be made for the function to execute!
# eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
# eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
@ext(handler=eh, op="get_rule")
def get_rule(attributes, region, prev_state):
    
    existing_rule_name = prev_state.get("props", {}).get("name")

    if existing_rule_name:
        # Try to get the rule. If you succeed, record the props and links from the current rule
        try:
            payload = {
                "Name": existing_rule_name
            }
            response = client.describe_rule(**payload)
            if response:
                eh.add_log("Got Rule", response)
                rule_name = response.get("Name")
                rule_arn = response.get("Arn")
                rule_role_arn = response.get("RoleArn")
                rule_event_bus_name = response.get("EventBusName")
                eh.add_state({"name": rule_name, "arn": rule_arn, "role_arn": rule_role_arn, "event_bus_name": rule_event_bus_name, "region": region})
                existing_props = {
                    "arn": rule_arn,
                    "name": rule_name,
                    "role_arn": rule_role_arn,
                    "event_bus_name": rule_event_bus_name
                }
                eh.add_props(existing_props)
                eh.add_links({"Rule": gen_rule_link(region, rule_name=rule_name, event_bus_name=rule_event_bus_name)})

                ### If the rule exists, then setup any followup tasks

                # Setup rule update
                comparable_attributes = {item: attributes[item] for item in attributes if item not in ["Tags", "Targets"]}
                comparable_response = {item: response[item] for item in response if item in comparable_attributes } # We only care when the values that are manually set by the user do not match
                if comparable_attributes != comparable_response:
                    eh.add_op("update_rule")

                # Setup tags update
                try:
                    # Try to get the current tags
                    response = client.list_tags_for_resource(ResourceARN=rule_arn)
                    eh.add_log("Got Tags", response)
                    relevant_items = response.get("Tags", [])

                    # Parse out the current tags
                    current_tags = {item.get("Key") : item.get("Value") for item in relevant_items}

                    # If there are tags specified, figure out which ones need to be added and which ones need to be removed
                    if attributes.get("Tags"):

                        tags = attributes.get("Tags")
                        formatted_tags = {item.get("Key") : item.get("Value") for item in tags}
                        # Compare the current tags to the desired tags
                        if formatted_tags != current_tags:
                            remove_tags = [k for k in current_tags.keys() if k not in formatted_tags]
                            add_tags = {k:v for k,v in formatted_tags.items() if v != current_tags.get(k)}
                            if remove_tags:
                                eh.add_op("remove_tags", remove_tags)
                            if add_tags:
                                eh.add_op("set_tags", add_tags)
                    # If there are no tags specified, make sure to remove any straggler tags
                    else:
                        if current_tags:
                            eh.add_op("remove_tags", list(current_tags.keys()))

                # If the rule does not exist, something has gone wrong. Probably don't permanently fail though, try to continue.
                except client.exceptions.ResourceNotFoundException:
                    eh.add_log("Rule Not Found", {"name": rule_name})
                    eh.retry_error("Rule Not Found -- Retrying", 20)
                except client.exceptions.InternalException as e:
                    eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
                    eh.retry_error("AWS Internal Error -- Retrying", 20)
                except ClientError as e:
                    handle_common_errors(e, eh, "Error Updating Rule Tags", progress=20)

                
                # Setup targets update
                try:
                    # Try to get the current targets
                    response = client.list_targets_by_rule(Rule=rule_name)
                    eh.add_log("Got Targets", response)
                    relevant_targets = response.get("Targets", [])

                    remove_targets = [target.get("Id") for target in relevant_targets if target.get("Id") not in attributes.get("Targets")]
                    if remove_targets:
                        eh.add_op("remove_targets", remove_targets)
                    
                    put_targets = [{**attributes.get("Targets").get(target), "id": target} for target in attributes.get("Targets") if target not in remove_targets]
                    formatted_put_targets = []
                    for item in put_targets:
                        formatted_target = remove_none_attributes({
                            'Id': item.get("id"),
                            'Arn': item.get("arn"),
                            'RoleArn': item.get("role_arn"),
                            'Input': item.get("input"),
                            'InputPath': item.get("input_path"),
                            'HttpParameters': remove_none_attributes({
                                'PathParameterValues': item.get("http_path_parameter_values") if item.get("http_path_parameter_values") else None,
                                'HeaderParameters': item.get("http_header_parameters") if item.get("http_header_parameters") else None,
                                'QueryStringParameters': item.get("http_query_string_parameters") if item.get("http_query_string_parameters") else None
                            }) if any( http_key in item for http_key in ["http_path_parameter_values", "http_header_parameters", "http_query_string_parameters"]) else None,
                            'DeadLetterConfig': {
                                'Arn': item.get("dead_letter_queue_arn")
                            } if item.get("dead_letter_queue_arn") else None,
                            'RetryPolicy': remove_none_attributes({
                                'MaximumRetryAttempts': item.get("maximum_retry_attempts"),
                                'MaximumEventAgeInSeconds': item.get("maximum_event_age_in_seconds")
                            }) if any( retry_key in item for retry_key in ["maximum_retry_attempts", "maximum_event_age_in_seconds"]) else None,
                        })
                        formatted_put_targets.append(formatted_target)

                    if formatted_put_targets:
                        eh.add_op("put_targets", formatted_put_targets)

                # If the rule does not exist, something has gone wrong. Probably don't permanently fail though, try to continue.
                except client.exceptions.ResourceNotFoundException:
                    eh.add_log("Rule Not Found", {"name": rule_name})
                    eh.retry_error("Rule Not Found -- Retrying", 25)
                except client.exceptions.InternalException as e:
                    eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
                    eh.retry_error("AWS Internal Error -- Retrying", 25)
                except ClientError as e:
                    handle_common_errors(e, eh, "Error Getting Rule Targets", progress=25)


            else:
                eh.add_log("Rule Does Not Exist", {"name": existing_rule_name})
                eh.add_op("create_rule")
                return 0
        # If there is no cache policy and there is an exception handle it here
        except client.exceptions.ResourceNotFoundException:
            eh.add_log("Rule Does Not Exist", {"name": existing_rule_name})
            eh.add_op("create_rule")
            return 0
        except client.exceptions.InternalException: # I believe this should not happen unless the plugin has insufficient permissions
            eh.add_log("AWS had an internal error. Working on handling this regardless.", {"name": existing_rule_name})
            eh.add_op("create_rule")
            return 0
        except ClientError as e:
            print(str(e))
            eh.add_log("Get Rule Error", {"error": str(e)}, is_error=True)
            eh.retry_error("Get Rule Error", 10)
            return 0
    else:
        eh.add_log("Rule Does Not Exist", {"name": existing_rule_name})
        eh.add_op("create_rule")
        return 0

            
@ext(handler=eh, op="create_rule")
def create_rule(attributes, region, prev_state):

    attributes_to_use = {item: attributes[item] for item in attributes if item not in ["Targets"]}

    try:
        response = client.put_rule(**attributes_to_use)
        eh.add_log("Created Rule", response)
        rule_name = attributes_to_use.get("Name")
        rule_arn = response.get("RuleArn")
        rule_role_arn = attributes_to_use.get("RoleArn")
        rule_event_bus_name = attributes_to_use.get("EventBusName") or "default"
        eh.add_state({"name": rule_name, "arn": rule_arn, "role_arn": rule_role_arn, "event_bus_name": rule_event_bus_name, "region": region})
        props_to_add = {
            "arn": rule_arn,
            "name": rule_name,
            "role_arn": rule_role_arn,
            "event_bus_name": rule_event_bus_name
        }
        eh.add_props(props_to_add)
        eh.add_links({"Rule": gen_rule_link(region, rule_name=rule_name, event_bus_name=rule_event_bus_name)})

        ### Once the rule exists, then setup any followup tasks

        # N/A, in the case of this plugin

    except client.exceptions.InvalidEventPatternException as e:
        eh.add_log(f"The event pattern specified is invalid. Please check your event pattern and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.LimitExceededException as e:
        eh.add_log(f"AWS Quota for EventBridge Rules reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of this Rule. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Rule", 20)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 20)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Rule", progress=20)


@ext(handler=eh, op="update_rule")
def update_rule(attributes, region, prev_state):

    attributes_to_use = {item: attributes[item] for item in attributes if item not in ["Tags", "Targets"]}
    existing_rule_name = eh.state["name"]
    existing_rule_role_arn = eh.state["role_arn"]
    existing_rule_event_bus_name = eh.state["event_bus_name"]

    try:
        response = client.put_rule(**attributes_to_use)
        eh.add_log("Updated Rule", response)
        rule_name = attributes.get("Name") or existing_rule_name
        rule_arn = response.get("RuleArn")
        rule_role_arn = attributes.get("RoleArn") or existing_rule_role_arn
        rule_event_bus_name = attributes.get("EventBusName") or existing_rule_event_bus_name or prev_state.get("props", {}).get("event_bus_name") 
        eh.add_state({"name": rule_name, "arn": rule_arn, "role_arn": rule_role_arn, "event_bus_name": rule_event_bus_name, "region": region})
        props_to_add = {
            "arn": rule_arn,
            "name": rule_name,
            "role_arn": rule_role_arn,
            "event_bus_name": rule_event_bus_name
        }
        eh.add_props(props_to_add)
        eh.add_links({"Rule": gen_rule_link(region, rule_name=rule_name, event_bus_name=rule_event_bus_name)})

        ### Once the rule exists, then setup any followup tasks

        # N/A, in the case of this plugin

    except client.exceptions.InvalidEventPatternException as e:
        eh.add_log(f"The event pattern specified is invalid. Please check your event pattern and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.LimitExceededException as e:
        eh.add_log(f"AWS Quota for EventBridge Rules reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of this Rule. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Rule", 20)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 20)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Rule", progress=20)


@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    rule_arn = eh.state["arn"]

    try:
        response = client.untag_resource(
            ResourceARN=rule_arn,
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)

    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of this Rule. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Rule", 80)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 80)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Rule Tags", progress=80)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")

    rule_arn = eh.state["arn"]
    try:
        response = client.tag_resource(
            ResourceARN=rule_arn,
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of this Rule. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Rule", 90)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 90)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)

@ext(handler=eh, op="put_targets")
def put_targets():

    put_targets = eh.ops.get('put_targets')
    rule_name= eh.state["name"]

    try:
        response = client.put_targets(
            Rule=rule_name,
            Targets=put_targets
        )
        print(response)
        if response.get("FailedEntryCount") > 0:
            failed_entries = response.get("FailedEntries")
            retry_codes = ["ResourceNotFoundException", "InternalException", "ConcurrentModificationException", "LimitExceededException"]
            if all([item.get("ErrorCode") not in retry_codes for item in failed_entries]):
                for item in failed_entries:
                    eh.add_log(f"The target {item.get('TargetId')} was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
                eh.perm_error(str(e), 80)
            else:
                for item in failed_entries:
                    eh.add_log(f"The target {item.get('TargetId')} was not added to the rule due to error code {item.get('ErrorCode')} and error message {item.get('ErrorMessage')}. Retrying.", {"error": str(e)}, is_error=True)
                eh.retry_error(f"Retrying Errors: {', '.join([item.get('ErrorCode') for item in failed_entries])}", 80)

        eh.add_log("Put Targets", put_targets)
        eh.add_op("add_permissions_for_targets", put_targets)


    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of the Target. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Target", 80)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 80)
    except client.exceptions.LimitExceededException as e:
        eh.add_log(f"AWS Quota for EventBridge Rules/Targets reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Rule Targets", progress=80)


@ext(handler=eh, op="remove_targets")
def remove_targets():
    remove_targets = eh.ops.get('remove_targets')
    rule_name= eh.state["name"]
    event_bus_name= eh.state["event_bus_name"]

    try:
        response = client.remove_targets(
            Rule=rule_name,
            EventBusName=event_bus_name,
            Ids=remove_targets,
            Force=False
        )
        print(response)
        if response.get("FailedEntryCount") > 0:
            failed_entries = response.get("FailedEntries")
            retry_codes = ["InternalException", "ConcurrentModificationException"]
            if all([item.get("ErrorCode") == "ResourceNotFoundException" for item in failed_entries]):
                eh.add_log(f"Rule/Event Bus combination Not Found. Targets Already Deleted.", {"error": str(e)}, is_error=True)
                return 0
            elif all([item.get("ErrorCode") == "ManagedRuleException" for item in failed_entries]):
                for item in failed_entries:
                    eh.add_log(f"The rule {rule_name} specified for the target {item.get('TargetId')} was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
                eh.perm_error(str(e), 80)
            else:
                for item in failed_entries:
                    eh.add_log(f"The target {item.get('TargetId')} was not removed from the rule due to error code {item.get('ErrorCode')} and error message {item.get('ErrorMessage')}. Retrying.", {"error": str(e)}, is_error=True)
                eh.retry_error(f"Retrying Errors: {', '.join([item.get('ErrorCode') for item in failed_entries])}", 80)

        eh.add_log("Removed Targets", remove_targets)


    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of the Target. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Target", 80)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 80)
    except client.exceptions.LimitExceededException as e:
        eh.add_log(f"AWS Quota for EventBridge Rules/Targets reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Rule Targets", progress=80)



@ext(handler=eh, op="add_permissions_for_targets")
def add_permissions_for_targets():
    targets_to_add_permissions_to = eh.ops.get('add_permissions_for_targets')
    rule_name= eh.state["name"]
    event_bus_name= eh.state["event_bus_name"]
    
    lambdas_to_add_permissions_to = []
    sns_topics_to_add_permissions_to = []
    sqs_queues_to_add_permissions_to = []
    for target in targets_to_add_permissions_to:
        target_type = analyze_type_of_arn(target.get("Arn"))
        if target_type == "lambda": 
            lambdas_to_add_permissions_to.append(target.get("Arn"))
        elif target_type == "sns":
            sns_topics_to_add_permissions_to.append(target.get("Arn"))
        elif target_type == "sqs": 
            sqs_queues_to_add_permissions_to.append(target.get("Arn"))
        else:
            # Do nothing, they are either unsupported or (way more likely) using role_arn for permissions
            pass
    
    if lambdas_to_add_permissions_to:
        eh.add_op("add_lambda_permissions", lambdas_to_add_permissions_to)
    if sns_topics_to_add_permissions_to:
        eh.add_op("add_sns_permissions", sns_topics_to_add_permissions_to)
    if sqs_queues_to_add_permissions_to:
        eh.add_op("add_sqs_permissions", sqs_queues_to_add_permissions_to)

    return 0


@ext(handler=eh, op="add_lambda_permissions")
def add_lambda_permissions(account_number):

    lambda_client = boto3.client("lambda")
    lambdas = eh.ops['add_lambda_permissions']
    rule_name = eh.state["name"]
    rule_arn = eh.state["arn"]

    for l in lambdas:
        try:
            response = lambda_client.add_permission(
                FunctionName=l,
                StatementId=f"EB_INVOKE_{rule_name}",
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceAccount=str(account_number),
                SourceArn=rule_arn
            )
            eh.add_log("Added Permission to Lambda", response)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceConflictException':
                pass
            else:
                raise e

@ext(handler=eh, op="add_sns_permissions")
def add_sns_permissions():

    sns_client = boto3.client('sns')
    sns_topics = eh.ops['add_sns_permissions']

    for t in sns_topics:
        try:
            statement_id = "PublishEventsToMyTopic"
            statement_to_add = {
                "Sid": statement_id,
                "Effect": "Allow",
                "Principal": {
                    "Service": "events.amazonaws.com"
                },
                "Action": "sns:Publish",
                "Resource": t
            }
            # Get the current policy
            response = sns_client.get_topic_attributes(
                TopicArn=t
            )
            # Format the policy with the new statement (as needed)
            existing_policy = json.loads(response.get("Attributes", {}).get("Policy", "{}"))
            existing_policy_statements = existing_policy.get("Statement")
            existing_policy_statements_to_add_to = [item for item in existing_policy_statements if item.get("Sid") != statement_id]
            all_statements = [*existing_policy_statements_to_add_to, statement_to_add]
            existing_policy["Statement"] = all_statements
            # Save the modified policy
            response = sns_client.set_topic_attributes(
                TopicArn=t,
                AttributeName='Policy',
                AttributeValue=json.dumps(existing_policy)
            )
            eh.add_log(f"Added Permission for Rule to Target SNS Topic", response)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceConflictException':
                pass
            else:
                raise e
            
@ext(handler=eh, op="add_sqs_permissions")
def add_sqs_permissions(account_number):

    sqs_client = boto3.client('sqs')
    sqs_queues = eh.ops['add_sqs_permissions']

    region = eh.state["region"]
    rule_name = eh.state["name"]
    rule_event_bus_name = eh.state["event_bus_name"]
    
    sqs_queue_formatted_rule_arn = f"arn:aws:events:{region}:{account_number}:rule/{rule_event_bus_name}/{rule_name}"

    for q in sqs_queues:
        try:
            statement_id = f"AWSEvents_{rule_name}"
            statement_to_add = {
                "Sid": statement_id,
                "Effect": "Allow",
                "Principal": {
                    "Service": "events.amazonaws.com"
                },
                "Action": "sqs:SendMessage",
                "Resource": q,
                "Condition": {
                    "ArnEquals": {
                        "aws:SourceArn": sqs_queue_formatted_rule_arn
                    }
                }
            }
            # Figure out the queue url from the arn
            queue_name = q.split(":")[5]
            response = sqs_client.get_queue_url(
                QueueName=queue_name
            )
            queue_url = response.get("QueueUrl")
            # Get the current queue policy
            response = sqs_client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=['Policy']
            )
            # Format the policy with the new statement
            existing_policy = json.loads(response.get("Attributes", {}).get("Policy", "{}"))
            existing_policy_statements = existing_policy.get("Statement", [])
            existing_policy_statements_to_add_to = [item for item in existing_policy_statements if item.get("Sid") != statement_id]
            all_statements = [*existing_policy_statements_to_add_to, statement_to_add]
            existing_policy["Statement"] = all_statements

            formatted_attributes = {
                "Policy": json.dumps(existing_policy)
            }
            # Set the modified policy on the sqs queue
            response = sqs_client.set_queue_attributes(
                QueueUrl=queue_url,
                Attributes=formatted_attributes
            )
            eh.add_log(f"Added Permission for Rule to Target SQS Queue", response)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ResourceConflictException':
                pass
            else:
                raise e

@ext(handler=eh, op="delete_rule")
def delete_rule():
    existing_rule_name = eh.state["name"]

    try:
        payload = {
            "Name": existing_rule_name
        }
        response = client.delete_rule(**payload)
        eh.add_log("Rule Deleted", {"name": existing_rule_name})

    except client.exceptions.ConcurrentModificationException as e:
        eh.add_log(f"Concurrent modification of this Rule. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("Concurrent modification of Rule", 80)
    except client.exceptions.ManagedRuleException as e:
        eh.add_log(f"This rule was created by an AWS service on behalf of your account. It is managed by that service and editing/deleting it is restricted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.InternalException as e:
        eh.add_log(f"AWS had an internal error. Retrying.", {"error": str(e)}, is_error=True)
        eh.retry_error("AWS Internal Error -- Retrying", 80)
    except client.exceptions.ResourceNotFoundException as e:
        eh.add_log(f"Rule Not Found", {"error": str(e)}, is_error=True)
        return 0
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Rule", progress=80)
    

def gen_rule_link(region, rule_name, event_bus_name):
    return f"https://{region}.console.aws.amazon.com/events/home?region={region}#/eventbus/{event_bus_name}/rules/{rule_name}"


def analyze_type_of_arn(arn):
    ### https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-use-resource-based.html
    # API GATEWAY section is out of date. This works based on RoleArn now.
    # Also, NOT supporting Cloudwatch Logs for this first iteration. You can have 10 statements total for all of cloudwatch's resource policy per region. Terrible. Call a lambda instead unless someone badly wants this.
    if arn.startswith("arn:aws:lambda"):
        return "lambda"
    elif arn.startswith("arn:aws:sns"):
        return "sns"
    elif arn.startswith("arn:aws:sqs"):
        return "sqs"
    else:
        return "not_supported_or_unnecessary"