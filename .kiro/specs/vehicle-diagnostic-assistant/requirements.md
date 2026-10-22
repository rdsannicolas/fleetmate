# Requirements Document

## Introduction

The Vehicle Diagnostic Assistant is an AI-powered feature that helps customers quickly identify possible vehicle problems through a conversational interface. Customers describe vehicle symptoms in natural language, and the system uses an AI model to generate a probable issue and a repair recommendation. All interactions are persisted for analytics and future reference. This document defines the functional and quality requirements for the feature using EARS patterns and INCOSE quality rules.

## Glossary

- **Diagnostic_Assistant**: The overall system that accepts symptom descriptions and returns AI-generated diagnostic results through a conversational interface.
- **Conversation_Manager**: The component that manages a multi-turn conversation session between a Customer and the Diagnostic_Assistant.
- **Symptom_Description**: Natural-language text submitted by a Customer that describes observed vehicle behavior or problems.
- **AI_Engine**: The component that sends Symptom_Descriptions to the underlying AI model and returns a structured Diagnostic_Result.
- **Diagnostic_Result**: The structured output produced by the AI_Engine, containing a Probable_Issue and a Repair_Recommendation.
- **Probable_Issue**: A natural-language statement describing the most likely vehicle problem based on the submitted symptoms.
- **Repair_Recommendation**: A natural-language statement describing suggested actions to resolve the Probable_Issue.
- **Interaction_Record**: A stored record of a single request-response exchange, including the Symptom_Description, Diagnostic_Result, timestamp, and session identifier.
- **Interaction_Store**: The persistence component that saves and retrieves Interaction_Records for analytics and future reference.
- **Customer**: A user who interacts with the Diagnostic_Assistant to diagnose a vehicle problem.
- **Session**: A single conversational exchange context identified by a unique Session identifier.
- **Web_Client**: The browser-based single-page application through which a Customer interacts with the Diagnostic_Assistant and its REST API.
- **Hosted_UI**: The Amazon Cognito-managed authentication interface that presents login and sign-up pages, hosted on a Cognito-provided domain.
- **Diagnostic_API**: The existing deployed REST API (API Gateway and Lambda) that exposes the `POST /diagnostics/analyze` and `GET /diagnostics/history` endpoints under the `/v1` stage.
- **Authorization_Code_Flow**: The OAuth 2.0 authorization-code grant with Proof Key for Code Exchange (PKCE) used by the Web_Client to authenticate a Customer through the Hosted_UI.
- **ID_Token**: The JSON Web Token issued by Amazon Cognito that identifies an authenticated Customer.
- **Access_Token**: The JSON Web Token issued by Amazon Cognito that the Web_Client presents as a bearer credential when calling the Diagnostic_API.
- **Sign_Up**: The self-service registration process, presented through the Hosted_UI, by which a new Customer creates an account and verifies ownership of an email address.
- **Static_Hosting**: The Amazon S3 bucket that stores the compiled Web_Client assets as static files.
- **Content_Delivery**: The Amazon CloudFront distribution that serves the Web_Client assets to Customers over HTTPS from the Static_Hosting origin.
- **Default_CloudFront_Domain**: The CloudFront-provided distribution domain name of the form `*.cloudfront.net` used to reach the Web_Client without custom DNS.
- **Cognito_Domain**: The Cognito-provided Hosted_UI domain of the form `*.auth.us-east-1.amazoncognito.com`.
- **App_Client**: The Amazon Cognito User Pool application client used by the Web_Client, configured with the Authorization_Code_Flow, callback URL, and logout URL.
- **Customer_Identity**: The unique identifier of an authenticated Customer, derived from the Amazon Cognito `sub` claim of the validated ID_Token and used by the Diagnostic_API as the `customerId` that scopes Interaction_Records to their owning Customer.
- **Current_Session**: The Session the Customer is interacting with at the time of a request, identified by the active Session identifier, used to determine which Interaction_Records to exclude when resolving the Most_Recent_Prior_Interaction.
- **Prior_Interaction**: An Interaction_Record owned by the authenticated Customer whose Session identifier is different from the Current_Session identifier.
- **Most_Recent_Prior_Interaction**: The single Prior_Interaction with the latest creation timestamp among all Prior_Interactions owned by the authenticated Customer; ties on creation timestamp are broken by the greater Interaction_Record identifier.

## Requirements

### Requirement 1: Submit Vehicle Symptoms

**User Story:** As a Customer, I want to describe my vehicle symptoms in a conversational interface, so that I can get help identifying the problem.

#### Acceptance Criteria

1. WHEN a Customer submits a Symptom_Description containing between 1 and 2000 characters, THE Diagnostic_Assistant SHALL accept the Symptom_Description, associate it with the active Session, and return an acknowledgment indicating the Symptom_Description was received.
2. IF a Customer submits a Symptom_Description that is empty or contains only whitespace characters, THEN THE Diagnostic_Assistant SHALL reject the submission, return a message requesting a non-empty description, and preserve the active Session without recording the rejected submission.
3. IF a Customer submits a Symptom_Description that exceeds 2000 characters, THEN THE Diagnostic_Assistant SHALL reject the submission, return a message stating the maximum length is 2000 characters, and preserve the active Session without recording the rejected submission.
4. WHERE no active Session exists for a Customer WHEN the Customer submits a Symptom_Description, THE Conversation_Manager SHALL create a new Session with a unique Session identifier before the Diagnostic_Assistant processes the Symptom_Description.
5. WHEN the Diagnostic_Assistant accepts a Symptom_Description, THE Diagnostic_Assistant SHALL complete processing and return a response within 5 seconds.

### Requirement 2: Generate Diagnostic Result

**User Story:** As a Customer, I want the system to analyze my symptoms, so that I receive a probable issue and a repair recommendation.

#### Acceptance Criteria

1. WHEN a Symptom_Description containing between 1 and 2000 characters of non-whitespace text is received, THE AI_Engine SHALL generate a Diagnostic_Result containing a Probable_Issue and a Repair_Recommendation.
2. THE Diagnostic_Result SHALL include exactly one Probable_Issue and exactly one Repair_Recommendation.
3. WHEN a Diagnostic_Result is generated, THE Diagnostic_Assistant SHALL return the Probable_Issue and the Repair_Recommendation to the Customer.
4. IF a received Symptom_Description is empty, contains only whitespace, or exceeds 2000 characters, THEN THE Diagnostic_Assistant SHALL reject the request without generating a Diagnostic_Result and return a message indicating the Symptom_Description is invalid.
5. IF the AI_Engine cannot determine a Probable_Issue from the Symptom_Description, THEN THE AI_Engine SHALL return a Diagnostic_Result that contains no Probable_Issue and includes an indication, observable to the Customer, that additional Symptom_Description detail is required to proceed.
6. IF the AI_Engine fails to produce a Diagnostic_Result within 30 seconds, THEN THE Diagnostic_Assistant SHALL return a message indicating the request could not be completed and prompt the Customer to retry.

### Requirement 3: Conversational Multi-Turn Interaction

**User Story:** As a Customer, I want to continue the conversation with follow-up messages, so that I can refine the diagnosis with additional details.

#### Acceptance Criteria

1. WHEN a Customer submits a follow-up Symptom_Description within an active Session, THE Conversation_Manager SHALL include all prior Interaction_Records of the active Session, in ascending chronological order, as context for the AI_Engine.
2. IF a follow-up Symptom_Description is submitted that is empty or exceeds 2000 characters, THEN THE Conversation_Manager SHALL reject the submission, return an error indication describing the invalid length, and preserve the existing Session context unchanged.
3. WHILE a Session is active, THE Conversation_Manager SHALL maintain the ordered sequence of Symptom_Descriptions and Diagnostic_Results for that Session in ascending chronological order of submission.
4. IF the number of Interaction_Records in an active Session reaches 50, THEN THE Conversation_Manager SHALL reject further follow-up submissions and return an error indication that the Session interaction limit has been reached.
5. WHEN a Session has had no Customer submission for 30 continuous minutes, THE Conversation_Manager SHALL mark the Session as closed and reject subsequent follow-up Symptom_Descriptions for that Session with an error indication that the Session is closed.

### Requirement 4: Store Interactions for Analytics

**User Story:** As a business analyst, I want all interactions to be stored, so that I can analyze diagnostic activity and reference past interactions.

#### Acceptance Criteria

1. WHEN a Diagnostic_Result is returned to a Customer, THE Interaction_Store SHALL persist, within 2 seconds, an Interaction_Record containing the Symptom_Description, the Diagnostic_Result, the Session identifier, and a creation timestamp recorded in UTC with millisecond precision.
2. THE Interaction_Store SHALL assign each persisted Interaction_Record a unique identifier that is distinct from the identifier of every other Interaction_Record.
3. IF persisting an Interaction_Record fails, THEN THE Interaction_Store SHALL discard the partial Interaction_Record, log the failure with the Session identifier and the creation timestamp, and return an error indication to the caller.
4. WHEN an authorized analyst requests Interaction_Records for a date range, THE Interaction_Store SHALL return all Interaction_Records with a creation timestamp greater than or equal to the start of the range and less than or equal to the end of the range, in ascending order of creation timestamp.
5. IF an analyst who is not authorized requests Interaction_Records, THEN THE Interaction_Store SHALL return an error indicating access is denied and SHALL return no Interaction_Records.
6. IF a requested date range has a start timestamp later than its end timestamp, THEN THE Interaction_Store SHALL reject the request and return an error indicating the date range is invalid.

### Requirement 5: Retrieve Past Interactions

**User Story:** As a Customer, I want to view my past diagnostic interactions, so that I can reference previous results.

#### Acceptance Criteria

1. WHEN a Customer requests the history for a Session, THE Interaction_Store SHALL return all Interaction_Records associated with that Session in ascending order of creation timestamp within 2 seconds.
2. IF a Customer requests the history for a Session identifier that has no associated Interaction_Records, THEN THE Interaction_Store SHALL return an empty result set.
3. IF a Customer requests the history for a Session identifier that does not match any existing Session, THEN THE Interaction_Store SHALL return an error indicating the Session was not found and SHALL return no Interaction_Records.
4. IF a Customer requests the history for a Session that the Customer is not authorized to access, THEN THE Interaction_Store SHALL return an error indicating access is denied and SHALL return no Interaction_Records.
5. WHERE the number of Interaction_Records for a Session exceeds 50, THE Interaction_Store SHALL return the Interaction_Records in pages of a maximum of 50 records per page.

### Requirement 6: Authenticate via Cognito Hosted UI

**User Story:** As a Customer, I want to log in through the Cognito-managed Hosted UI, so that I can access the Diagnostic_Assistant without the Web_Client handling my credentials.

#### Acceptance Criteria

1. WHEN an unauthenticated Customer requests a protected view in the Web_Client, THE Web_Client SHALL redirect the Customer to the Hosted_UI using the Authorization_Code_Flow.
2. WHEN the Hosted_UI returns an authorization code to the Web_Client callback URL, THE Web_Client SHALL exchange the authorization code for an ID_Token and an Access_Token using the Proof Key for Code Exchange verifier within 10 seconds.
3. WHEN the Web_Client holds a valid Access_Token, THE Web_Client SHALL include the Access_Token as a bearer credential in the Authorization header of every request to the Diagnostic_API.
4. IF the Hosted_UI returns an authentication error instead of an authorization code, THEN THE Web_Client SHALL display a message stating that login did not complete and SHALL present an option to retry login.
5. WHILE the Customer holds no valid ID_Token, THE Web_Client SHALL restrict access to the symptom submission view and the session history view.
6. WHERE the App_Client is configured, THE App_Client SHALL enable the Authorization_Code_Flow and register the Content_Delivery callback URL and logout URL.
7. IF the authorization code exchange returns an error or does not complete within 10 seconds, THEN THE Web_Client SHALL discard any partial token, SHALL display a message stating that login did not complete, and SHALL present an option to retry login.

### Requirement 7: Self-Service Sign-Up

**User Story:** As a new Customer, I want to register myself through the Hosted UI, so that I can create an account and then log in.

#### Acceptance Criteria

1. WHEN a Customer selects the sign-up option in the Hosted_UI, THE Hosted_UI SHALL present a Sign_Up form that collects an email address and a password of at least 8 characters.
2. WHEN a Customer submits a syntactically valid email address that is not already registered together with a password of at least 8 characters, THE Hosted_UI SHALL create an unconfirmed account and send an email verification message to the submitted email address within 60 seconds.
3. IF a Customer submits an email address that is not syntactically valid, an email address that is already registered, or a password shorter than 8 characters, THEN THE Hosted_UI SHALL reject the Sign_Up, SHALL NOT create an account, and SHALL present an indication of which submitted value is invalid.
4. WHEN a Customer submits the correct email verification value from the verification message, THE Hosted_UI SHALL confirm the account and permit the Customer to log in.
5. IF a Customer submits an incorrect email verification value, THEN THE Hosted_UI SHALL leave the account unconfirmed and SHALL present a message stating that the verification value is incorrect.
6. IF a Customer attempts to log in with an unconfirmed account, THEN THE Hosted_UI SHALL deny access and present a message stating that email verification is required.
7. WHEN a Customer completes Sign_Up and email verification, THE Web_Client SHALL allow the Customer to authenticate through the Authorization_Code_Flow.

### Requirement 8: Submit a Symptom and View the Diagnosis

**User Story:** As a Customer, I want to enter vehicle symptoms in the web interface and see the diagnosis, so that I can identify the probable issue and repair recommendation.

#### Acceptance Criteria

1. WHILE the Customer holds a valid Access_Token, THE Web_Client SHALL present an input that accepts a Symptom_Description of 1 to 2000 characters and a submit control, and SHALL keep the submit control disabled while the input is empty, contains only whitespace, or exceeds 2000 characters.
2. WHEN the Customer submits a Symptom_Description of 1 to 2000 characters, THE Web_Client SHALL send the Symptom_Description to the `POST /diagnostics/analyze` endpoint of the Diagnostic_API.
3. WHEN the Diagnostic_API returns a Diagnostic_Result that contains a Probable_Issue and a Repair_Recommendation, THE Web_Client SHALL display the Probable_Issue, the Repair_Recommendation, the estimated cost, and the confidence score.
4. WHEN the Diagnostic_API returns a response indicating that additional symptom detail is required, THE Web_Client SHALL display the needs-more-detail message and retain the submitted Symptom_Description for editing.
5. IF the Diagnostic_API returns a 422 response, THEN THE Web_Client SHALL display the returned suggestion value and SHALL retain the submitted Symptom_Description for correction.
6. IF the Diagnostic_API returns a 503 response, THEN THE Web_Client SHALL display a message stating that the diagnosis service is temporarily unavailable and SHALL present an option to retry.
7. WHILE a submitted Symptom_Description is awaiting a response from the Diagnostic_API, THE Web_Client SHALL indicate that the request is in progress and SHALL prevent duplicate submission of the same Symptom_Description.
8. IF no response to a submitted Symptom_Description is received from the Diagnostic_API within 30 seconds, THEN THE Web_Client SHALL stop indicating that the request is in progress, SHALL display a message stating that the request did not complete, SHALL present an option to retry, and SHALL retain the submitted Symptom_Description.

### Requirement 9: View Session History

**User Story:** As a Customer, I want to view my own session history in the web interface, so that I can reference previous diagnostic results.

#### Acceptance Criteria

1. WHEN a Customer opens the session history view, THE Web_Client SHALL request Interaction_Records from the `GET /diagnostics/history` endpoint using the Customer Session identifier.
2. WHEN the Diagnostic_API returns Interaction_Records, THE Web_Client SHALL display the Interaction_Records in ascending order of creation timestamp, ordering any records that share a creation timestamp by their Interaction_Record identifier.
3. WHEN the Diagnostic_API returns an empty set of Interaction_Records, THE Web_Client SHALL display a message stating that no past interactions exist for the Session.
4. IF the Diagnostic_API returns a 403 response for a history request, THEN THE Web_Client SHALL display a message stating that the Customer is not authorized to view the requested Session history and SHALL retain any previously displayed Interaction_Records.
5. IF the history request fails due to a service error, a network failure, or no response within 10 seconds, THEN THE Web_Client SHALL display a message stating that the history could not be retrieved and SHALL present a control to retry the request up to a maximum of 3 attempts.
6. WHERE the Diagnostic_API response includes a next-page token, THE Web_Client SHALL present a control that requests the next page of up to 50 Interaction_Records.

### Requirement 10: Token Handling, Expiry, and Logout

**User Story:** As a Customer, I want my authenticated session to be managed securely, so that expired tokens and logout are handled correctly.

#### Acceptance Criteria

1. IF the Diagnostic_API returns a 401 response, THEN THE Web_Client SHALL discard the stored ID_Token and Access_Token within 1 second of receiving the response, and redirect the Customer to the Hosted_UI to re-authenticate.
2. WHEN a Customer selects the logout control, THE Web_Client SHALL discard the stored ID_Token and Access_Token and redirect the Customer to the Hosted_UI logout URL.
3. WHEN the Hosted_UI completes logout and redirects to the registered logout URL, THE Web_Client SHALL present the unauthenticated view and SHALL confirm that no ID_Token or Access_Token remains in client-side storage.
4. WHILE the Customer holds a valid ID_Token, THE Web_Client SHALL retain the ID_Token and Access_Token only for the duration of the authenticated browser session, and SHALL discard both tokens when the browser session ends.
5. IF a stored ID_Token has an expiry timestamp that is at or before the current time, THEN THE Web_Client SHALL treat the ID_Token as invalid, discard the stored ID_Token and Access_Token, and redirect the Customer to the Hosted_UI to re-authenticate.
6. IF discarding the stored ID_Token or Access_Token fails, THEN THE Web_Client SHALL present the unauthenticated view and SHALL NOT include either token in any subsequent request to the Diagnostic_API.

### Requirement 11: Static Web Hosting on S3 and CloudFront

**User Story:** As an operator, I want the web application hosted on AWS serverless static hosting, so that Customers can reach a scalable single-page application over HTTPS.

#### Acceptance Criteria

1. THE Static_Hosting SHALL store the compiled Web_Client assets as static files.
2. WHEN a Customer requests the Web_Client over the Default_CloudFront_Domain using HTTPS, THE Content_Delivery SHALL serve the requested Web_Client assets from the Static_Hosting origin with a success response.
3. WHEN a Customer requests a navigational Web_Client route that does not correspond to a stored asset, THE Content_Delivery SHALL return the single-page application entry document with a success response so that client-side routing resolves the route.
4. IF a request reaches the Static_Hosting origin directly rather than through the Content_Delivery distribution, THEN THE Static_Hosting SHALL deny the request with an access-denied response so that the Static_Hosting assets are reachable only through the Content_Delivery distribution.
5. IF a Customer requests the Web_Client over the Default_CloudFront_Domain using HTTP, THEN THE Content_Delivery SHALL redirect the request to the equivalent HTTPS request.

### Requirement 12: Minimal Domain and Certificate Configuration

**User Story:** As an operator, I want the web application to use AWS-provided default domains, so that the deployment avoids custom DNS and certificate management.

#### Acceptance Criteria

1. THE Content_Delivery SHALL serve the Web_Client using the Default_CloudFront_Domain.
2. THE Hosted_UI SHALL be served using the Cognito_Domain.
3. THE Web_Client deployment SHALL operate without any custom Domain Name System record, without Amazon Route 53 configuration, and without a custom Transport Layer Security certificate.
4. WHERE the App_Client registers a callback URL and a logout URL, THE App_Client SHALL register those URLs using the Default_CloudFront_Domain over HTTPS.

### Requirement 13: Retrieve the Most-Recent Prior Interaction

**User Story:** As a Customer, I want to see my single most-recent diagnostic interaction from before my current session, so that I can recall what I last reported without browsing my full history.

#### Acceptance Criteria

1. WHEN an authenticated Customer requests the most-recent prior interaction, THE Diagnostic_API SHALL return exactly one Interaction_Record, the Most_Recent_Prior_Interaction, resolved from the Interaction_Records owned by the Customer_Identity whose Session identifier differs from the Current_Session identifier.
2. WHEN the Diagnostic_API returns the Most_Recent_Prior_Interaction, THE Diagnostic_API SHALL include the Symptom_Description and the Diagnostic_Result of that Interaction_Record using the same Interaction_Record shape used for session history retrieval.
3. WHEN more than one Prior_Interaction exists for the Customer_Identity, THE Diagnostic_API SHALL select the Prior_Interaction with the latest creation timestamp, breaking a tie on creation timestamp by selecting the greater Interaction_Record identifier.
4. THE Diagnostic_API SHALL resolve the Most_Recent_Prior_Interaction only from Interaction_Records owned by the Customer_Identity derived from the validated ID_Token, and SHALL exclude every Interaction_Record owned by any other Customer_Identity from the result.
5. IF the Customer_Identity owns no Interaction_Record whose Session identifier differs from the Current_Session identifier, THEN THE Diagnostic_API SHALL return an empty result containing no Interaction_Record.
6. IF a request for the most-recent prior interaction omits the Current_Session identifier, THEN THE Diagnostic_API SHALL reject the request and return an error indicating the Current_Session identifier is required.
7. WHEN a Customer opens the authenticated view that displays the prior interaction, THE Web_Client SHALL request the Most_Recent_Prior_Interaction from the Diagnostic_API using the Current_Session identifier and SHALL display the returned Symptom_Description and Diagnostic_Result in a panel distinct from the Current_Session history view.
8. WHEN the Diagnostic_API returns an empty result for the most-recent prior interaction, THE Web_Client SHALL display a message stating that no previous interaction exists.
9. IF the Diagnostic_API returns a 401 response to a request for the most-recent prior interaction, THEN THE Web_Client SHALL discard the stored ID_Token and Access_Token and redirect the Customer to the Hosted_UI to re-authenticate.
10. IF the request for the most-recent prior interaction fails due to a service error, a network failure, or no response within 10 seconds, THEN THE Web_Client SHALL display a message stating that the previous interaction could not be retrieved and SHALL present a control to retry the request.
