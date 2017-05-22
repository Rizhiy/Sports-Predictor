import pickle
import random
import sys
from copy import deepcopy

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from keras.layers.core import K
from scipy.interpolate import spline

from prediction_model import SESSION, MATCH_LIST, BATCH_SIZE, PLAYERS_PER_TEAM, NUM_OF_TEAMS, RETRAIN, PLAYER_GAMES, \
    BOSTON_MAJOR_LAST_GAME_ID, TI_6_LAST_GAME_ID, DEBUG, PLAYER_PERFORMANCES, PLAYER_SKILLS, PLAYER_DIM, FEAR_ID
from prediction_model.Graphs import density_hist
from prediction_model.match_processing_model import player_results_split, player_to_results_param0, \
    player_to_results_param1, \
    loss as inference_loss, player_performance as match_performances, player_skills, player_results, \
    team_results, player_to_team_nn, player_result_nn, team_result_nn, player_performance_estimate
from prediction_model.skill_update_model import loss as update_loss, player_pre_skill, player_performance, \
    player_next_performance, post_skill, player_post_skill
from prediction_model.utils import create_data_set, get_new_batch, store_player_performances, get_skill_batch, \
    update_player_skills, create_player_games, get_test_batch, split_list, make_mu_and_sigma, make_k_and_theta, \
    create_validation_sets, reset_stats, get_skill

test_result = player_results_split[0]
test2_result = player_to_results_param0[0] * player_to_results_param1[0]

inference_train_step = tf.train.AdamOptimizer().minimize(inference_loss)
update_train_step = tf.train.AdamOptimizer().minimize(update_loss)
init = tf.global_variables_initializer()

create_data_set()

match_list = [x for x in MATCH_LIST if BOSTON_MAJOR_LAST_GAME_ID > x > int(BOSTON_MAJOR_LAST_GAME_ID - 4 * 1e8)]

validation_list, t_list = split_list(match_list, 0.9)
if TI_6_LAST_GAME_ID in t_list:
    t_list.remove(TI_6_LAST_GAME_ID)
    validation_list.append(TI_6_LAST_GAME_ID)
cross_validation_set = create_validation_sets(validation_list)
num_t_batches = int(len(t_list) / BATCH_SIZE)

file_name = "predictions.pkl"

if RETRAIN:
    for idx, test_list in enumerate(cross_validation_set):
        # Cross validation
        SESSION.run(init)  # Reset model
        train_set = deepcopy(cross_validation_set)
        test_list = train_set[0]
        del train_set[0]
        train_list = [item for sublist in train_set for item in sublist]
        num_train_batches = int(len(train_list) / BATCH_SIZE)
        reset_stats()  # Reset player skills
        create_player_games(train_list)

        pass_num = 0
        result = 0
        alpha = .9
        phase = 30
        counter = 0
        min_update_loss = np.infty
        stopping_counter = 0
        stopping_counter2 = 0
        timer = 0
        max_accuracy = 0
        while True:
            if timer > 100:
                break
            if stopping_counter > 4:
                # Evaluate system
                K.set_learning_phase(False)
                num_test_batches = int(len(test_list) / BATCH_SIZE)
                predicted = []
                result = []
                error = []
                for seed in range(num_test_batches):
                    batch = get_test_batch(seed, test_list)
                    predicted_player_skills = np.array(batch["player_skills"])
                    player_skills_split = tf.split(predicted_player_skills, PLAYERS_PER_TEAM * NUM_OF_TEAMS, axis=1)
                    for i in range(len(player_skills_split)):
                        player_skills_split[i] = tf.squeeze(player_skills_split[i], 1)
                        player_skills_split[i] = tf.cast(player_skills_split[i], tf.float32)
                        player_skills_split[i], _ = tf.split(player_skills_split[i], 2, axis=1)
                    team0_input = []
                    team1_input = []
                    for i in range(PLAYERS_PER_TEAM):
                        team0_input.append(player_skills_split[i])
                        team1_input.append(player_skills_split[PLAYERS_PER_TEAM + i])
                    team0_input = tf.concat(team0_input, axis=1)
                    team1_input = tf.concat(team1_input, axis=1)
                    team0_skill, _ = make_mu_and_sigma(player_to_team_nn, team0_input)
                    team1_skill, _ = make_mu_and_sigma(player_to_team_nn, team1_input)
                    predicted_player_result = []
                    for i in range(PLAYERS_PER_TEAM * NUM_OF_TEAMS):
                        if i < PLAYERS_PER_TEAM:
                            team_skills = tf.concat([team0_skill, team1_skill], axis=1)
                        else:
                            team_skills = tf.concat([team1_skill, team0_skill], axis=1)
                        player_to_result_input = tf.concat([player_skills_split[i], team_skills], axis=1)
                        param0, param1 = make_k_and_theta(player_result_nn, player_to_result_input)
                        predicted_player_result.append(param0 * param1)
                    predicted_team_result = tf.sigmoid(team_result_nn(tf.concat([team0_skill, team1_skill], axis=1)))

                    predicted_result = SESSION.run(predicted_player_result)
                    predicted_result = np.swapaxes(predicted_result, 0, 1)
                    for i in range(len(predicted_result)):
                        for player in range(len(predicted_result[i])):
                            predicted.append(predicted_result[i][player])
                            result.append(batch["player_results"][i][player])
                            error.append(batch["player_results"][i][player] - predicted_result[i][player])
                data = {"predicted": np.array(predicted), "result": np.array(result), "error": np.array(error)}
                pickle.dump(data, open(file_name, "wb"))
                prediction_error = data["error"]
                prediction_result = data["result"]
                print("Round: {}".format(timer))
                if DEBUG > 1:
                    print("Error std:    {}".format(np.std(prediction_error, 0)))
                    print("Original std: {}".format(np.std(prediction_result, 0)))
                accuracy = np.mean(1 - np.var(prediction_error, 0) / np.var(prediction_result, 0))
                if accuracy < max_accuracy:
                    stopping_counter2 += 1
                else:
                    stopping_counter2 = 0
                    max_accuracy = accuracy
                if stopping_counter2 > 1 and timer > 2:
                    break
                print(accuracy)
                result = 0
                timer += 1
                phase = 20
                stopping_counter = 0
                K.set_learning_phase(True)
            if phase > 0:
                # Train prediction model
                batch = get_new_batch(counter, train_list, num_train_batches)
                _, loss_step, player_performances, performance_estimate, test, test2 = SESSION.run(
                    (inference_train_step, inference_loss, match_performances, player_performance_estimate, test_result,
                     test2_result),
                    feed_dict={player_skills: batch["player_skills"],
                               player_results: batch["player_results"],
                               team_results: batch["team_results"]})
                result = (result * alpha + loss_step) / (1 + alpha)
                store_player_performances(batch["match_ids"], np.swapaxes(player_performances, 0, 1))
                if batch["switch"]:
                    if DEBUG > 1:
                        print("Phase: {}".format(phase))
                    random.shuffle(train_list)
                    phase -= 1
                    pass_num += 1
                    if DEBUG > 2:
                        print("Pass {}".format(pass_num))
                        print("Inference loss:\t\t{:4d}".format(int(result)))
                        print("Actual:      {}".format(test[0]))
                        print("Inferred:    {}".format(test2[0]))
                        print("Performance: {}".format(player_performances[0][0]))
                        print("Prior:       {}".format(batch["player_skills"][0][0]))
                    result = 0
                counter += 1
            else:
                # Train skill update model
                player_loss = []
                ids = list(PLAYER_GAMES.keys())
                random.shuffle(ids)
                for player_id in ids:
                    batch = get_skill_batch(player_id)
                    if len(batch["player_next_performance"]) == 0:
                        continue
                    _, loss_step, player_skills_new, skills_mu = SESSION.run((
                        update_train_step, update_loss, post_skill, player_post_skill),
                        feed_dict={player_pre_skill: batch["player_pre_skill"],
                                   player_performance: batch["player_performance"],
                                   player_next_performance: batch["player_next_performance"]})
                    player_loss.append(loss_step)
                    update_player_skills(player_id, batch["target_game"], player_skills_new, skills_mu)
                result = int(np.mean(player_loss))
                if result < min_update_loss:
                    stopping_counter = 0
                    min_update_loss = result
                else:
                    stopping_counter += 1
                if DEBUG > 2:
                    print("Skill update loss:\t{:4d}".format(int(result)))
            if np.math.isnan(loss_step):
                print("Nan loss", file=sys.stderr)
                break
    K.set_learning_phase(False)
    predicted = []
    result = []
    error = []
    for seed in range(num_t_batches):
        predicted_player_skills = np.array(batch["player_skills"])
        predicted_result = SESSION.run(predicted_player_result)
        predicted_result = np.swapaxes(predicted_result, 0, 1)
        for i in range(len(predicted_result)):
            for player in range(len(predicted_result[i])):
                predicted.append(predicted_result[i][player])
                result.append(batch["player_results"][i][player])
                error.append(batch["player_results"][i][player] - predicted_result[i][player])
    data = {"predicted": np.array(predicted), "result": np.array(result), "error": np.array(error)}
    pickle.dump(data, open(file_name, "wb"))
else:
    data = pickle.load(open(file_name, "rb"))


plt.figure(figsize=(12, 7.5))
plt.rc('text', usetex=True)
plt.rc('font', family='serif')
plt.rc('font', size=16)
if DEBUG > 1 :
    print(PLAYER_SKILLS[TI_6_LAST_GAME_ID])
    print(PLAYER_PERFORMANCES[TI_6_LAST_GAME_ID])
    predicted = np.swapaxes(data["predicted"], 0, 1)
    error = np.swapaxes(data["error"], 0, 1)
    result = np.swapaxes(data["result"], 0, 1)
    stat = 1
    density_hist(predicted[stat])
    density_hist(result[stat])
    # plt.legend()
    plt.figure()
    density_hist(error[stat])
    plt.show()

if DEBUG > 2:
    skills = []
    for match_id in PLAYER_GAMES[FEAR_ID]["all"]:
        skills.append(get_skill(FEAR_ID, match_id, True))
    skills = np.array(skills)

    l = len(skills[:, 0])
    xnew = np.linspace(0, l, 128)

    for i in range(PLAYER_DIM):
        plt.plot(xnew, spline(range(l), skills[:, i], xnew))
    plt.tick_params(axis='both', which='both', bottom='off', top='off', labelbottom='off', right='off', left='off',
                    labelleft='off')
    plt.xlabel("Time")
    plt.ylabel("Skill level")
plt.show()

error = data["error"]
result = data["result"]
print()
print()
print("Error variance:    {}".format(repr(np.var(error, 0))))
print("Original variance: {}".format(repr(np.var(result, 0))))
print("R^2:               {}".format(repr([1] * 8 - np.var(error, 0) / np.var(result, 0))))
